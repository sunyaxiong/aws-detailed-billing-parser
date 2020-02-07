# -*- coding: utf-8 -*-
#
# awsdbrparser/parser.py
#
# Copyright 2015 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from __future__ import print_function

import collections
import csv
import json
import threading
import time
import os

import boto3
import click
from elasticsearch import Elasticsearch, RequestsHttpConnection, helpers
from requests_aws4auth import AWS4Auth
import requests

from . import utils
from .config import PROCESS_BY_BULK, PROCESS_BY_LINE, PROCESS_BI_ONLY, HEADERS

Summary = collections.namedtuple('Summary', 'added skipped updated control_messages')
"""
Holds the summary of documents processed by the parser.
"""


class ParserError(Exception):
    pass


def analytics(config, echo):
    """
    This function generate extra information in elasticsearch analyzing the line items of the file
    :param echo:
    :param config:
    :return:
    """

    # Opening Input filename again to run in parallel
    file_in = open(config.input_filename, 'r')
    awsauth = None
    if config.awsauth:
        # 实验中使用AWS-cli将账单文件拷贝到proxy-server1本地目录未使用AWS的认证，
        # 所以ec2 proxy-server到ES服务是不需要认证的，可以略过此代码。
        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials:
            region = session.region_name
            awsauth = AWS4Auth(credentials.access_key, credentials.secret_key, region, 'es',
                               session_token=credentials.token)
    # 考虑在此处添加判断，是否向高版本的ES插入数据，在此之前还是先测试向本地es节点写入数据。
    # 经过测试，python版本的sdk对安全的支持文档较少，未找到合适方法使用sdk。为了后期维护和扩展性，
    # 此处为预先创建billing索引，创建mapping
    # 考虑使用官方更推荐的restfulAPI，对高版本、安全的ES集群进行数据写入。
    es = Elasticsearch([{'host': config.es_host, 'port': config.es_port}], timeout=config.es_timeout, http_auth=awsauth,
                       connection_class=RequestsHttpConnection)
    if not config.custom:
        es.indices.create(config.index_name, ignore=400)
        es.indices.create(config.es_doctype, ignore=400)
    else:
        echo("原始版本的两个create重复了，观察后再补充逻辑，The ES version is {}".format(config.es2))
        # TODO

    csv_file = csv.DictReader(file_in, delimiter=config.csv_delimiter)
    analytics_daytime = dict()
    analytics_day_only = dict()
    for recno, json_row in enumerate(csv_file):
        # Pre-Process the row to append extra information
        json_row = utils.pre_process(json_row)
        if is_control_message(json_row, config):
            # Skip this line
            continue
        elif json_row.get('ProductName') == 'Amazon Elastic Compute Cloud' and 'RunInstances' in json_row.get(
                'Operation') and json_row.get('UsageItem'):
            # Get the day time ('2016-03-01 01:00:00')
            daytime = json_row.get('UsageStartDate')
            # the day only '2016-03-01'
            day = json_row.get('UsageStartDate').split(' ')[0]
            # Add the day time to the dict
            analytics_daytime.setdefault(daytime, {"Count": 0, "Cost": 0.00, "RI": 0, "Spot": 0, "Unblended": 0.00})
            # Increment the count of total instances
            analytics_daytime[daytime]["Count"] += 1
            analytics_daytime[daytime]["Unblended"] += float(json_row.get('UnBlendedCost', 0.00))
            analytics_daytime[daytime]["Cost"] += float(json_row.get('Cost', 0.00))

            # Add the day only to the dict
            analytics_day_only.setdefault(day, {"Count": 0, "RI": 0, "Spot": 0, "Min": None, "Max": None})
            analytics_day_only[day]["Count"] += 1
            # Increment the count of RI or Spot if the instance is one or other
            if json_row.get('UsageItem') == 'Reserved Instance':
                analytics_day_only[day]["RI"] += 1
                analytics_daytime[daytime]["RI"] += 1
            elif json_row.get('UsageItem') == 'Spot Instance':
                analytics_day_only[day]["Spot"] += 1
                analytics_daytime[daytime]["Spot"] += 1

    # Some DBR files has Cost (Single Account) and some has (Un)BlendedCost (Consolidated Account)
    # In this case we try to process both, but one will be zero and we need to check
    # TODO: use a single variable and an flag to output Cost or Unblended
    # TODO 此处需要测试，ec2-per-usd是否能在es7下创建，对于此处的判断逻辑有疑惑：一个索引，多个type？
    # 此处的坑在export-cn.json 里，index已经创建了
    if config.es2:
        index_name = config.index_name
    else:
        index_name = 'ec2_per_usd'
    # if not es.indices.exists(index=index_name):  # 这是原始代码，改造后因为2.3与7.3索引用法升级，所以改造如下
    if not es.indices.exists(index="ec2_per_usd"):  # 如果ec2_per_usd不存在，创建index并mapping；
        if not config.custom:
            es.indices.create(index_name, ignore=400, body={
                "mappings": {
                    "ec2_per_usd": {
                        "properties": {
                            "UsageStartDate": {
                                "type": "date", "format": "YYYY-MM-dd HH:mm:ss||YYYY/MM/dd HH:mm||YYYY/M/d H:mm"
                            }
                        }
                    }
                }
            })
        else:
            echo("在-C参数控制下，通过rest创建索引ec2_per_usd, version {}".format(config.es2))
            url = "http://{}:{}/{}".format(config.es_host, config.es_port, "ec2_per_usd")
            r = requests.put(url, headers=HEADERS, json={"mappings": {
                "properties": {
                    "UsageStartDate": {
                        "type": "date",
                        "format": "YYYY/M/d HH:mm:ss||YYYY/MM/dd HH:mm||YYYY/M/d H:mm||YYYY/MM/dd HH:mm:ss"
                                "||YYYY-MM-dd HH:mm:ss"
                    }
                }
            }})
            if not r.ok:
                echo("ec2_per_usd的mapping创建失败，请检查{}".format(r.json()))
    for k, v in analytics_daytime.items():
        result_cost = 1.0 / (v.get('Cost') / v.get('Count')) if v.get('Cost') else 0.00
        result_unblended = 1.0 / (v.get('Unblended') / v.get('Count')) if v.get('Unblended') else 0.0
        if not config.custom:
            response = es.index(index=index_name, doc_type='ec2_per_usd',  # index的子type在2.3下可以这样用
                                body={'UsageStartDate': k,
                                      'EPU_Cost': result_cost,
                                      'EPU_UnBlended': result_unblended})
            if not response.get('created'):
                echo('[!] Unable to send document to ES!')
        else:
            echo("接下来写入ec2_per_usd索引数据, version {}".format(config.es2))
            url = "http://{}:{}/{}/_doc".format(config.es_host, config.es_port, "ec2_per_usd")
            r = requests.post(url, headers=HEADERS, json={
                'UsageStartDate': k,
                "EPU_Cost": result_cost,
                "EPU_UnBlended": result_unblended
            })
            if not r.ok:
                echo("ec2_per_usd索引数据写入失败，请检查: {}".format(r.json()))

    # Elasticity
    #
    # The calculation is 1 - min / max EC2 instances per day
    # The number of EC2 instances has been calculated previously
    #
    if config.es2:
        index_name = config.index_name
    else:
        index_name = 'elasticity'
    # if not es.indices.exists(index=index_name):  # 原始代码写法，因es7.3索引语法升级，改造判断为下方的写法
    if not es.indices.exists(index="elasticity"):  # True/False # 就是用来判断是否存在index，create的用法是2.3的特性
        if not config.custom:
            es.indices.create(index_name, ignore=400, body={
                "mappings": {
                    "elasticity": {
                        "properties": {
                            "UsageStartDate": {
                                "type": "date", "format": "YYYY-MM-dd HH:mm:ss||YYYY/MM/dd HH:mm||YYYY/M/d H:mm"
                            }
                        }
                    }
                }
            })
        else:
            # -C 参数控制
            url = "http://{}:{}/{}".format(config.es_host, config.es_port, "elasticity")
            r = requests.put(url, headers=HEADERS, json={
                "mappings": {
                    "properties": {
                        "UsageStartDate": {
                            "type": "date",
                            "format": "YYYY/M/d HH:mm:ss||YYYY/MM/dd HH:mm||YYYY/M/d H:mm||YYYY/MM/dd HH:mm:ss"
                                      "||YYYY-MM-dd HH:mm:ss"
                        }
                    }
                }
            })
            if not r.ok:
                echo("elasticity的索引mapping失败，请检查: {}".format(r.json()))
    for k, v in analytics_day_only.items():
        ec2_min = min(value["Count"] - value["RI"] for key, value in analytics_daytime.items() if k in key)
        ec2_max = max(value["Count"] - value["RI"] for key, value in analytics_daytime.items() if k in key)
        if ec2_max:
            elasticity = 1.0 - float(ec2_min) / float(ec2_max)
        else:
            elasticity = 1.0

        ri_coverage = float(analytics_day_only[k]["RI"]) / float(analytics_day_only[k]["Count"])
        spot_coverage = float(analytics_day_only[k]["Spot"]) / float(analytics_day_only[k]["Count"])

        if not config.custom:
            response = es.index(index=index_name, doc_type='elasticity',
                                body={'UsageStartDate': k + ' 12:00:00',
                                      'Elasticity': elasticity,
                                      'ReservedInstanceCoverage': ri_coverage,
                                      'SpotCoverage': spot_coverage})

            if not response.get('created'):
                echo('[!] Unable to send document to ES!')
        else:
            echo("此处通过rest创建elasticity的mapping， setter： config.es2: version {}".format(config.es2))
            url = "http://{}:{}/{}/_doc".format(config.es_host, config.es_port, "elasticity")
            r = requests.post(url, headers=HEADERS, json={
                'UsageStartDate': k + ' 12:00:00',
                "Elasticity": elasticity,
                "ReservedInstanceCoverage": ri_coverage,
                'SpotCoverage': spot_coverage
            })
            if not r.ok:
                echo("elasticity索引写入失败，请检查: {}".format(r.json()))

    file_in.close()
    # Finished Processing
    return


def parse(config, verbose=False):
    """

    :param verbose:
    :param config: An instance of :class:`~awsdbrparser.config.Config` class,
        used for parsing parametrization.

    :rtype: Summary
    """
    echo = utils.ClickEchoWrapper(quiet=(not verbose))


    echo('Opening input file: {}'.format(config.input_filename))
    file_in = open(config.input_filename, 'r')

    # 输出到文件和es节点两种情况
    if config.output_to_file:
        echo('Opening output file: {}'.format(config.output_filename))
        file_out = open(config.output_filename, 'w')

    elif config.output_to_elasticsearch:
        echo('Sending DBR to Elasticsearch host: {}:{}'.format(config.es_host, config.es_port))
        awsauth = None
        if config.awsauth:
            session = boto3.Session()
            credentials = session.get_credentials()
            if credentials:
                region = session.region_name
                awsauth = AWS4Auth(credentials.access_key, credentials.secret_key, region, 'es',
                                   session_token=credentials.token)

        es = Elasticsearch([{'host': config.es_host, 'port': config.es_port}], timeout=config.es_timeout,
                           http_auth=awsauth, connection_class=RequestsHttpConnection)
        if not config.custom:
            if config.delete_index:
                echo('Deleting current index: {}'.format(config.index_name))
                es.indices.delete(config.index_name, ignore=404)
            es.indices.create(config.index_name, ignore=400)
            es.indices.put_mapping(index=config.index_name, doc_type=config.es_doctype, body=config.mapping)
        else:
            # TODO es7 and security
            url = "http://{}:{}/{}".format(config.es_host, config.es_port, config.index_name)
            if config.delete_index:
                echo('Deleting current index: {}'.format(config.index_name))
                requests.delete(url, headers=HEADERS)
            if not es.indices.exists(index=config.index_name):
                echo("创建billing-*的index，更新mapping {}".format(config.es2))
                # 创建索引和mapping一起完成
                fp = os.path.join(os.path.dirname(__file__), "data", "dbr_doctype_es6x.json")
                mappings = {"mappings": json.load(open(fp))}
                r = requests.put(url, headers=HEADERS, json=utils.unicode_convert(mappings))
                if not r.ok:
                    echo("mappings: {}:{}".format(type(utils.unicode_convert(mappings)),
                                                  utils.unicode_convert(mappings)))
                    echo("billing索引mapping失败，请检查： {}".format(r.json()))

    # 日志的显示情况？
    if verbose:
        progressbar = click.progressbar

        # calculate number of rows in input file in preparation to display a progress bar
        record_count = sum(1 for _ in file_in) - 1
        file_in.seek(0)  # reset file descriptor

        echo("Input file has {} record(s)".format(record_count))

        if config.process_mode == PROCESS_BY_BULK:
            echo('Processing in BULK MODE, size: {}'.format(config.bulk_size))
        elif config.process_mode == PROCESS_BY_LINE:
            echo('Processing in LINE MODE')
        elif config.process_mode == PROCESS_BI_ONLY:
            if config.analytics:
                echo('Processing BI Only')
            else:
                echo("You don't have set the parameter -bi. Nothing to do.")
    else:
        # uses a 100% bug-free progressbar, guaranteed :-)
        progressbar = utils.null_progressbar
        record_count = 0

    # If BI is enabled, create a thread and start running 此处预先处理elasticity和ec2-per-usd两个索引
    analytics_start = time.time()
    if config.analytics:
        echo('Starting the BI Analytics Thread')
        thread = threading.Thread(target=analytics, args=(config, echo,))
        thread.start()

    added = skipped = updated = control = 0

    if config.process_mode == PROCESS_BY_BULK:
        with progressbar(length=record_count) as pbar:
            # If you wish to sort the records by UsageStartDate before send to
            # ES just uncomment the 2 lines below and comment the third line
            # reader = csv.DictReader(file_in, delimiter=config.csv_delimiter)
            # csv_file = sorted(reader, key=lambda line: line["UsageStartDate"]+line["UsageEndDate"])
            csv_file = csv.DictReader(file_in, delimiter=config.csv_delimiter)

            def documents():
                for json_row in csv_file:
                    if not is_control_message(json_row, config):
                        if config.debug:
                            print(json.dumps(  # do not use 'echo()' here
                                utils.pre_process(json_row)))
                        yield json.dumps(utils.pre_process(json_row))  # 此处对json_row进行了处理
                        pbar.update(1)

            # 此处在批量写入账单数据
            if not config.custom:
                for recno, (success, result) in enumerate(helpers.streaming_bulk(es, documents(),
                                                                                 index=config.index_name,
                                                                                 doc_type=config.es_doctype,
                                                                                 chunk_size=config.bulk_size)):
                    # <recno> integer, the record number (0-based)
                    # <success> bool
                    # <result> a dictionary like this one:
                    #
                    #   {
                    #       'create': {
                    #           'status': 201,
                    #           '_type': 'billing',
                    #           '_shards': {
                    #               'successful': 1,
                    #               'failed': 0,
                    #               'total': 2
                    #           },
                    #           '_index': 'billing-2015-12',
                    #           '_version': 1,
                    #           '_id': u'AVOmiEdSF_o3S6_4Qeur'
                    #       }
                    #   }
                    #
                    if not success:
                        message = 'Failed to index record {:d} with result: {!r}'.format(recno, result)
                        if config.fail_fast:
                            raise ParserError(message)
                        else:
                            echo(message, err=True)
                    else:
                        added += 1
            else:
                echo("custom streaming bulk, The ES version is {}".format(config.es2))
                for recno, (success, result) in enumerate(helpers.streaming_bulk(es, documents(),
                                                                                 index=config.index_name,
                                                                                 # doc_type=config.es_doctype,
                                                                                 chunk_size=config.bulk_size)):
                    if not success:
                        message = 'Failed to index record {:d} with result: {!r}'.format(recno, result)
                        if config.fail_fast:
                            raise ParserError(message)
                        else:
                            echo(message, err=True)
                    else:
                        added += 1
    elif config.process_mode == PROCESS_BY_LINE:
        # 忽略即可
        with progressbar(length=record_count) as pbar:
            csv_file = csv.DictReader(file_in, delimiter=config.csv_delimiter)
            for recno, json_row in enumerate(csv_file):
                if is_control_message(json_row, config):
                    control += 1
                else:
                    if config.debug:
                        print(json.dumps(  # do not use 'echo()' here
                            utils.pre_process(json_row),
                            ensure_ascii=False))

                    if config.output_to_file:
                        file_out.write(
                            json.dumps(utils.pre_process(json_row), ensure_ascii=False))
                        file_out.write('\n')
                        added += 1

                    elif config.output_to_elasticsearch:
                        if config.check:
                            # FIXME: the way it was, `search_exists` will not suffice, since we'll need the document _id for the update operation; # noqa
                            # FIXME: use `es.search` with the following sample body: `{'query': {'match': {'RecordId': '43347302922535274380046564'}}}`; # noqa
                            # SEE: https://elasticsearch-py.readthedocs.org/en/master/api.html#elasticsearch.Elasticsearch.search; # noqa
                            response = es.search_exists(index=config.es_doctype, doc_type=config.es_doctype,
                                                        q='RecordId:{}'.format(json_row['RecordId']))
                            if response:
                                if config.update:
                                    # TODO: requires _id from the existing document
                                    # FIXME: requires use of `es.search` method instead of `es.search_exists`
                                    # SEE: https://elasticsearch-py.readthedocs.org/en/master/api.html#elasticsearch.Elasticsearch.update; # noqa
                                    skipped += 1
                                else:
                                    skipped += 1
                            else:
                                response = es.index(index=config.es_doctype, doc_type=config.es_doctype,
                                                    body=body_dump(json_row, config))
                                if not es_index_successful(response):
                                    message = 'Failed to index record {:d} with result {!r}'.format(recno, response)
                                    if config.fail_fast:
                                        raise ParserError(message)
                                    else:
                                        echo(message, err=True)
                                else:
                                    added += 1
                        else:
                            response = es.index(index=config.es_doctype, doc_type=config.es_doctype,
                                                body=body_dump(json_row, config))
                            if not es_index_successful(response):
                                message = 'Failed to index record {:d} with result {!r}'.format(recno, response)
                                if config.fail_fast:
                                    raise ParserError(message)
                                else:
                                    echo(message, err=True)
                            else:
                                added += 1

                pbar.update(1)
    elif config.process_mode == PROCESS_BI_ONLY and config.analytics:
        # 忽略即可
        echo('Processing Analytics Only')
        while thread.is_alive():
            # Wait for a timeout
            analytics_now = time.time()
            if analytics_start - analytics_now > config.analytics_timeout * 60:
                echo('Analytics processing timeout. exiting')
                break
            time.sleep(5)

    else:
        echo('Nothing to do!')

    file_in.close()

    if config.output_to_file:
        file_out.close()

    echo('Finished processing!')
    echo('')

    # the first line is the header then is skipped by the count bellow
    echo('Summary of documents processed...')
    echo('           Added: {}'.format(added))
    echo('         Skipped: {}'.format(skipped))
    echo('         Updated: {}'.format(updated))
    echo('Control messages: {}'.format(control))
    echo('')

    return Summary(added, skipped, updated, control)


def is_control_message(record, config):
    # <record> record dict
    # <config> an instance of `awsdbrparser.config.Config`
    # data = json.dumps(record, ensure_ascii=False)
    return utils.bulk_data(record, config.bulk_msg)


def body_dump(record, config):
    # <record> record dict
    # <config> an instance of `awsdbrparser.config.Config`
    body = json.dumps(utils.pre_process(record), ensure_ascii=False)
    return body


def es_index_successful(response):
    """
    Test if an Elasticsearch client ``index`` method response indicates a
    successful index operation. The response parameter should be a dictionary
    with following keys:

    .. sourcecode:: python

        {
            '_shards': {
                'total': 2,
                'failed': 0,
                'successful': 1
            },
            '_index': u'billing-2015-12',
            '_type': u'billing',
            '_id': u'AVOmKFXgF_o3S6_4PkP1',
            '_version': 1,
            'created': True
        }

    According to `Elasticsearch Index API <https://www.elastic.co/guide/en/
    elasticsearch/reference/current/docs-index_.html>`, an index operation is
    successful in the case ``successful`` is at least 1.

    :rtype: bool
    """
    return response.get('_shards', {}).get('successful', 0) >= 1
