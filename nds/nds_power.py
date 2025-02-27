#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-FileCopyrightText: Copyright (c) 2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# -----
#
# Certain portions of the contents of this file are derived from TPC-DS version 3.2.0
# (retrieved from www.tpc.org/tpc_documents_current_versions/current_specifications5.asp).
# Such portions are subject to copyrights held by Transaction Processing Performance Council (“TPC”)
# and licensed under the TPC EULA (a copy of which accompanies this file as “TPC EULA” and is also
# available at http://www.tpc.org/tpc_documents_current_versions/current_specifications5.asp) (the “TPC EULA”).
#
# You may not use this file except in compliance with the TPC EULA.
# DISCLAIMER: Portions of this file is derived from the TPC-DS Benchmark and as such any results
# obtained using this file are not comparable to published TPC-DS Benchmark results, as the results
# obtained from using this file do not comply with the TPC-DS Benchmark.
#

import argparse
import csv
import os
import time
from collections import OrderedDict
from pyspark.sql import SparkSession
from PysparkBenchReport import PysparkBenchReport
from pyspark.sql import DataFrame

from check import check_json_summary_folder, check_query_subset_exists, check_version
from nds_gen_query_stream import split_special_query
from nds_schema import get_schemas

check_version()


def gen_sql_from_stream(query_stream_file_path):
    """Read Spark compatible query stream and split them one by one

    Args:
        query_stream_file_path (str): path of query stream generated by TPC-DS tool

    Returns:
        ordered dict: an ordered dict of {query_name: query content} query pairs
    """
    with open(query_stream_file_path, 'r') as f:
        stream = f.read()
    all_queries = stream.split('-- start')[1:]
    # split query in query14, query23, query24, query39
    extended_queries = OrderedDict()
    for q in all_queries:
        # e.g. "-- start query 32 in stream 0 using template query98.tpl"
        query_name = q[q.find('template')+9: q.find('.tpl')]
        if 'select' in q.split(';')[1]:
            part_1, part_2 = split_special_query(q)
            extended_queries[query_name + '_part1'] = part_1
            extended_queries[query_name + '_part2'] = part_2
        else:
            extended_queries[query_name] = q

    # add "-- start" string back to each query
    for q_name, q_content in extended_queries.items():
        extended_queries[q_name] = '-- start' + q_content
    return extended_queries

def setup_tables(spark_session, input_prefix, input_format, use_decimal, execution_time_list):
    """set up data tables in Spark before running the Power Run queries.

    Args:
        spark_session (SparkSession): a SparkSession instance to run queries.
        input_prefix (str): path of input data.
        input_format (str): type of input data source, e.g. parquet, orc, csv, json.
        use_decimal (bool): use decimal type for certain columns when loading data of text type.
        execution_time_list ([(str, str, int)]): a list to record query and its execution time.

    Returns:
        execution_time_list: a list recording query execution time.
    """
    spark_app_id = spark_session.sparkContext.applicationId
    # Create TempView for tables
    for table_name in get_schemas(False).keys():
        start = int(time.time() * 1000)
        table_path = input_prefix + '/' + table_name
        reader =  spark_session.read.format(input_format)
        if input_format in ['csv', 'json']:
            reader = reader.schema(get_schemas(use_decimal)[table_name])
        reader.load(table_path).createOrReplaceTempView(table_name)
        end = int(time.time() * 1000)
        print("====== Creating TempView for table {} ======".format(table_name))
        print("Time taken: {} millis for table {}".format(end - start, table_name))
        execution_time_list.append(
            (spark_app_id, "CreateTempView {}".format(table_name), end - start))
    return execution_time_list

def register_delta_tables(spark_session, input_prefix, execution_time_list):
    spark_app_id = spark_session.sparkContext.applicationId
    # Register tables for Delta Lake
    for table_name in get_schemas(False).keys():
        start = int(time.time() * 1000)
        # input_prefix must be absolute path: https://github.com/delta-io/delta/issues/555
        register_sql = f"CREATE TABLE IF NOT EXISTS {table_name} USING DELTA LOCATION '{input_prefix}/{table_name}'"
        print(register_sql)
        spark_session.sql(register_sql)
        end = int(time.time() * 1000)
        print("====== Registering for table {} ======".format(table_name))
        print("Time taken: {} millis for table {}".format(end - start, table_name))
        execution_time_list.append(
            (spark_app_id, "Register {}".format(table_name), end - start))
    return execution_time_list


def run_one_query(spark_session,
                  query,
                  query_name,
                  output_path,
                  output_format):
    df = spark_session.sql(query)
    if not output_path:
        df.collect()
    else:
        ensure_valid_column_names(df).write.format(output_format).mode('overwrite').save(
                output_path + '/' + query_name)

def ensure_valid_column_names(df: DataFrame):
    def is_column_start(char):
        return char.isalpha() or char == '_'

    def is_column_part(char):
        return char.isalpha() or char.isdigit() or char == '_'

    def is_valid(column_name):
        return len(column_name) > 0 and is_column_start(column_name[0]) and all(
            [is_column_part(char) for char in column_name[1:]])

    def make_valid(column_name):
        # To simplify: replace all invalid char with '_'
        valid_name = ''
        if is_column_start(column_name[0]):
            valid_name += column_name[0]
        else:
            valid_name += '_'
        for char in column_name[1:]:
            if not is_column_part(char):
                valid_name += '_'
            else:
                valid_name += char
        return valid_name

    def deduplicate(column_names):
        # In some queries like q35, it's possible to get columns with the same name. Append a number
        # suffix to resolve this problem.
        dedup_col_names = []
        for i,v in enumerate(column_names):
            count = column_names.count(v)
            index = column_names[:i].count(v)
            dedup_col_names.append(v+str(index) if count > 1 else v)
        return dedup_col_names

    valid_col_names = [c if is_valid(c) else make_valid(c) for c in df.columns]
    dedup_col_names = deduplicate(valid_col_names)
    return df.toDF(*dedup_col_names)

def get_query_subset(query_dict, subset):
    """Get a subset of queries from query_dict.
    The subset is specified by a list of query names.
    """
    check_query_subset_exists(query_dict, subset)
    return dict((k, query_dict[k]) for k in subset)


def run_query_stream(input_prefix,
                     property_file,
                     query_dict,
                     time_log_output_path,
                     extra_time_log_output_path,
                     sub_queries,
                     input_format="parquet",
                     use_decimal=True,
                     output_path=None,
                     output_format="parquet",
                     json_summary_folder=None,
                     delta_unmanaged=False,
                     keep_sc=False,
                     hive_external=False):
    """run SQL in Spark and record execution time log. The execution time log is saved as a CSV file
    for easy accesibility. TempView Creation time is also recorded.

    Args:
        input_prefix (str): path of input data or warehouse if input_format is "iceberg" or hive_external=True.
        query_dict (OrderedDict): ordered dict {query_name: query_content} of all TPC-DS queries runnable in Spark
        time_log_output_path (str): path of the log that contains query execution time, both local
                                    and HDFS path are supported.
        input_format (str, optional): type of input data source.
        use_deciaml(bool, optional): use decimal type for certain columns when loading data of text type.
        output_path (str, optional): path of query output, optinal. If not specified, collect()
                                     action will be applied to each query. Defaults to None.
        output_format (str, optional): query output format, choices are csv, orc, parquet. Defaults
        to "parquet".
    """
    execution_time_list = []
    total_time_start = time.time()
    # check if it's running specific query or Power Run
    if len(query_dict) == 1:
        app_name = "NDS - " + list(query_dict.keys())[0]
    else:
        app_name = "NDS - Power Run"
    # Execute Power Run or Specific query in Spark
    # build Spark Session
    session_builder = SparkSession.builder
    if property_file:
        spark_properties = load_properties(property_file)
        for k,v in spark_properties.items():
            session_builder = session_builder.config(k,v)
    if input_format == 'iceberg':
        session_builder.config("spark.sql.catalog.spark_catalog.warehouse", input_prefix)
    if input_format == 'delta' and not delta_unmanaged:
        session_builder.config("spark.sql.warehouse.dir", input_prefix)
        session_builder.enableHiveSupport()
    if hive_external:
        session_builder.enableHiveSupport()

    spark_session = session_builder.appName(
        app_name).getOrCreate()
    if hive_external:
        spark_session.catalog.setCurrentDatabase(input_prefix)

    if input_format == 'delta' and delta_unmanaged:
        # Register tables for Delta Lake. This is only needed for unmanaged tables.
        execution_time_list = register_delta_tables(spark_session, input_prefix, execution_time_list)
    spark_app_id = spark_session.sparkContext.applicationId
    if input_format != 'iceberg' and input_format != 'delta' and not hive_external:
        execution_time_list = setup_tables(spark_session, input_prefix, input_format, use_decimal,
                                           execution_time_list)

    check_json_summary_folder(json_summary_folder)
    if sub_queries:
        query_dict = get_query_subset(query_dict, sub_queries)
    # Run query
    power_start = int(time.time())
    for query_name, q_content in query_dict.items():
        # show query name in Spark web UI
        spark_session.sparkContext.setJobGroup(query_name, query_name)
        print("====== Run {} ======".format(query_name))
        q_report = PysparkBenchReport(spark_session)
        summary = q_report.report_on(run_one_query,spark_session,
                                                   q_content,
                                                   query_name,
                                                   output_path,
                                                   output_format)
        print(f"Time taken: {summary['queryTimes']} millis for {query_name}")
        query_times = summary['queryTimes']
        execution_time_list.append((spark_app_id, query_name, query_times[0]))
        if json_summary_folder:
            # property_file e.g.: "property/aqe-on.properties" or just "aqe-off.properties"
            if property_file:
                summary_prefix = os.path.join(
                    json_summary_folder, os.path.basename(property_file).split('.')[0])
            else:
                summary_prefix =  os.path.join(json_summary_folder, '')
            q_report.write_summary(query_name, prefix=summary_prefix)
    power_end = int(time.time())
    power_elapse = int((power_end - power_start)*1000)
    if not keep_sc:
        spark_session.sparkContext.stop()
    total_time_end = time.time()
    total_elapse = int((total_time_end - total_time_start)*1000)
    print("====== Power Test Time: {} milliseconds ======".format(power_elapse))
    print("====== Total Time: {} milliseconds ======".format(total_elapse))
    execution_time_list.append(
        (spark_app_id, "Power Start Time", power_start))
    execution_time_list.append(
        (spark_app_id, "Power End Time", power_end))
    execution_time_list.append(
        (spark_app_id, "Power Test Time", power_elapse))
    execution_time_list.append(
        (spark_app_id, "Total Time", total_elapse))

    header = ["application_id", "query", "time/milliseconds"]
    # print to driver stdout for quick view
    print(header)
    for row in execution_time_list:
        print(row)
    # write to local file at driver node
    with open(time_log_output_path, 'w', encoding='UTF8') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(execution_time_list)
    # write to csv in cloud environment
    if extra_time_log_output_path:
        spark_session = SparkSession.builder.getOrCreate()
        time_df = spark_session.createDataFrame(data=execution_time_list, schema = header)
        time_df.coalesce(1).write.csv(extra_time_log_output_path)

def load_properties(filename):
    myvars = {}
    with open(filename) as myfile:
        for line in myfile:
            name, var = line.partition("=")[::2]
            myvars[name.strip()] = var.strip()
    return myvars

if __name__ == "__main__":
    parser = parser = argparse.ArgumentParser()
    parser.add_argument('input_prefix',
                        help='text to prepend to every input file path (e.g., "hdfs:///ds-generated-data"). ' +
                        'If --hive or if input_format is "iceberg", this argument will be regarded as the value of property ' +
                        '"spark.sql.catalog.spark_catalog.warehouse". Only default Spark catalog ' +
                        'session name "spark_catalog" is supported now, customized catalog is not ' +
                        'yet supported. Note if this points to a Delta Lake table, the path must be ' +
                        'absolute. Issue: https://github.com/delta-io/delta/issues/555')
    parser.add_argument('query_stream_file',
                        help='query stream file that contains NDS queries in specific order')
    parser.add_argument('time_log',
                        help='path to execution time log, only support local path.',
                        default="")
    parser.add_argument('--input_format',
                        help='type for input data source, e.g. parquet, orc, json, csv or iceberg, delta. ' +
                        'Certain types are not fully supported by GPU reading, please refer to ' +
                        'https://github.com/NVIDIA/spark-rapids/blob/branch-22.08/docs/compatibility.md ' +
                        'for more details.',
                        choices=['parquet', 'orc', 'avro', 'csv', 'json', 'iceberg', 'delta'],
                        default='parquet')
    parser.add_argument('--output_prefix',
                        help='text to prepend to every output file (e.g., "hdfs:///ds-parquet")')
    parser.add_argument('--output_format',
                        help='type of query output',
                        default='parquet')
    parser.add_argument('--property_file',
                        help='property file for Spark configuration.')
    parser.add_argument('--floats',
                        action='store_true',
                        help='When loading Text files like json and csv, schemas are required to ' +
                        'determine if certain parts of the data are read as decimal type or not. '+
                        'If specified, float data will be used.')
    parser.add_argument('--json_summary_folder',
                        help='Empty folder/path (will create if not exist) to save JSON summary file for each query.')
    parser.add_argument('--delta_unmanaged',
                        action='store_true',
                        help='Use unmanaged tables for DeltaLake. This is useful for testing DeltaLake without ' +
        '               leveraging a Metastore service.')
    parser.add_argument('--keep_sc',
                        action='store_true',
                        help='Keep SparkContext alive after running all queries. This is a ' +
                        'limitation on Databricks runtime environment. User should always attach ' +
                        'this flag when running on Databricks.')
    parser.add_argument('--hive',
                        action='store_true',
                        help='use table meta information in Hive metastore directly without ' +
                        'registering temp views.')
    parser.add_argument('--extra_time_log',
                        help='extra path to save time log when running in cloud environment where '+
                        'driver node/pod cannot be accessed easily. User needs to add essential extra ' +
                        'jars and configurations to access different cloud storage systems. ' +
                        'e.g. s3, gs etc.')

    parser.add_argument('--sub_queries',
                        type=lambda s: [x.strip() for x in s.split(',')],
                        help='comma separated list of queries to run. If not specified, all queries ' +
                        'in the stream file will be run. e.g. "query1,query2,query3". Note, use ' +
                        '"_part1" and "_part2" suffix for the following query names: ' +
                        'query14, query23, query24, query39. e.g. query14_part1, query39_part2')
    args = parser.parse_args()
    query_dict = gen_sql_from_stream(args.query_stream_file)
    run_query_stream(args.input_prefix,
                     args.property_file,
                     query_dict,
                     args.time_log,
                     args.extra_time_log,
                     args.sub_queries,
                     args.input_format,
                     not args.floats,
                     args.output_prefix,
                     args.output_format,
                     args.json_summary_folder,
                     args.delta_unmanaged,
                     args.keep_sc,
                     args.hive)
