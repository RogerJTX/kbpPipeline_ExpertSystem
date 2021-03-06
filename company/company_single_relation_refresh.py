#!/home/apollo/anaconda3/bin/python3
#-*- coding: utf-8 -*-
#******************************************************************************
# Author       : jtx
# Create       : 2020-03-31 19:05
# Last modified: 2020-05-27 14:32
# Filename     : company_single_relation_refresh.py
# Description  : 模型更新后手动运行，对特定产业的-企业产业领域分类手动全量更新
#******************************************************************************

import configparser
import sys
from pymongo import MongoClient
from pymongo import errors
from pyArango.connection import Connection as ArangoConnection
from pyArango.theExceptions import AQLFetchError
import pymysql
import Threading
from dateutil import parser
import datetime
import json
import logging
import re
import copy
import requests
import os

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(filename)s[line:%(lineno)d] - %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

dir_path = os.path.dirname(__file__)
kbp_path = os.path.dirname(dir_path)
config_path = os.path.join(kbp_path,"config.ini")

def set_log():
    logging.basicConfig(level=logging.INFO) 
    file_log_handler = RotatingFileHandler(os.path.join(dir_path,"industry_refresh_log.txt"), maxBytes=1024 * 1024 * 300, backupCount=10)
    formatter = logging.Formatter('%(asctime)s - %(filename)s - %(lineno)s - %(levelname)s - %(message)s')
    file_log_handler.setFormatter(formatter)
    logging.getLogger().addHandler(file_log_handler)

class RelationPipeline(object):

    def __init__(self):

        self.config = configparser.ConfigParser()
        self.config.read(config_path)
        self.arango_con = ArangoConnection(arangoURL=self.config.get("arango","arango_url"),username= self.config.get("arango","user"),password=self.config.get("arango","passwd"))
        self.arango_db = self.arango_con[self.config.get("arango","db")]
        self.kb_company = self.arango_db[self.config.get("arango","kb_company")]
        self.industry_url = self.config.get("url","company_classifier")
        self._init_division_schema() # init division_schema from mysql
        self._init_industry_schema()
        self.count_graph_update = 0 # arango更新关系数据数量
        self.total = 0 # 处理日期总共需要添加关系的数量

    def _init_division_schema(self):
        '''
        行政区域实体关系加载
        '''
        self.division_schema = {}
        sql_conn = pymysql.connect( host = self.config.get("mysql","host") ,
                            user = self.config.get("mysql","user") ,
                            passwd = self.config.get("mysql","passwd"),
                            port = self.config.getint("mysql","port") ,
                            db = self.config.get("mysql","db"),
                            charset = "utf8" )
        sql_cur = sql_conn.cursor() 

        # 初始化行政区域的关系schema
        sql_query_industry = "select name, id, level, parent_id from {}".format(self.config.get("mysql","res_division"))
        sql_cur.execute(sql_query_industry)
        divisions = sql_cur.fetchall()
        for division in divisions:
            division_name, division_id, division_level, division_parent_id = division
            self.division_schema[division_name] = {
                "relation_type":"concept_relation/100004",
                "object_name":division_name,
                "object_type": "division",
                "object_id": division_id
            }

        sql_cur.close()
        sql_conn.close()
        logger.info("MYSQL division schema 加载完成")

    def _init_industry_schema(self):
        '''
        init loading industry schema at mysql res_industry table
        '''
        self.industry_schema = {}
        sql_conn = pymysql.connect( host = self.config.get("mysql","host") ,
                            user = self.config.get("mysql","user") ,
                            passwd = self.config.get("mysql","passwd"),
                            port = self.config.getint("mysql","port") ,
                            db = self.config.get("mysql","db"),
                            charset = "utf8" )
        sql_cur = sql_conn.cursor() 

        ###################################################################
        self.all_industries = ["生物制药","医疗器械"]#设置需要单个或多个更新的产业分类
        ###################################################################
        self.update_industries_id = []
        # 初始化产业/产业领域 schema
        sql_query_industry = "select name, id, parent_id from {}".format(self.config.get("mysql","res_industry"))
        sql_cur.execute(sql_query_industry)
        labels = sql_cur.fetchall()
        for industry in labels:
            industry_name, industry_id, parent_id = industry
            self.industry_schema[industry_id] = {
                "relation_type":"concept_relation/100011",
                "object_name":industry_name,
                "object_type": "industry",
                "object_id": industry_id,
                "object_parent_id": parent_id
            }
            if industry_name in self.all_industries:
                self.update_industries_id.append(industry_id)

        sql_cur.close()
        sql_conn.close()
        logger.info("MYSQL industry schema 加载完成")



    def get_related_industry_tags(self, industry_id):
        '''
        根据子领域名称递归返回领域及所有父领域标签
        '''
        relations = []
        # 过滤招商领域与图谱定义不一致的
        if not industry_id in self.industry_schema:
            return relations
        
        relations.append(self.industry_schema[industry_id])
        parent_id = self.industry_schema[industry_id]["object_parent_id"]
        while (parent_id):
            node = self.industry_schema[parent_id]
            relations.append(node)
            parent_id = node["object_parent_id"]
        return relations


    def query_process_company(self, process_date):
        
        if process_date == "yesterday":
            process_date = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        
        elif process_date == "today":
            process_date = datetime.today().strftime("%Y-%m-%d")
            
        elif len(process_date.split("-")) == 3:
            process_date = process_date
            
        else:
            raise Exception("无效参数")
        
        self.process_date = process_date
        iso_date_str = process_date + 'T00:00:00+08:00'
        iso_date = parser.parse(iso_date_str)

        aql = "FOR company IN {} filter company.create_time>='2020-04-24 12:57:37.408114' SORT company.create_time return company".format(
                        self.config.get("arango","kb_company"), iso_date) 
        
        try:
            res = self.arango_db.fetch_list(aql)
        except AQLFetchError as e:
            '''没有查到数据时，fetch_list会抛出异常'''
            res = []
            logger.warn("Arango企业库没有查到数据",e)

        self.total = len(res)           
        logger.info("[{}]，企业知识库查到待处理数据[{}]个".format(process_date, self.total))
        return res


    def process_division_rel(self, properties):
        div_rel = []
        province = properties["province"]
        city = properties["city"]
        area = properties["area"]

        if province and province in self.division_schema.keys():
            if province in ["北京市","上海市","重庆市","天津市"]:
                province = province.replace("市","")
            div_rel.append(self.division_schema[province])

        if city and city in self.division_schema.keys():
            div_rel.append(self.division_schema[city])

        if area and area in self.division_schema.keys():
            div_rel.append(self.division_schema[area])

        return div_rel

        
    def process_industry_rel(self,properties):
        '''
        产业领域标签ID化添加
        '''
        industry_tags = []

        industry_field_tags = []
        company = properties["name"]
        #all_industries = ["人工智能","光电产业","新能源汽车","医疗器械"]#2020-05-27up
        post_data = {
            "company_list": [ company ],
            "industry_list":self.all_industries
        }
        if not self.all_industries:#没有特殊指定产业，默认为全量
            post_data['industry_list'] = 'all'
        try:
            res = requests.post(self.industry_url, data=json.dumps(post_data))

            if res.status_code == 200:
                tags = res.json().get("body")[0]
                industry_field_tags.extend(tags)
                logger.info("接口数据返回分类=[{}]".format(industry_field_tags))

        except Exception as e:
            logging.error("获取公司产业领域失败，公司名=[{}]，接口=[{}]".format(company,self.industry_url),e)

        for field in industry_field_tags:
            for industry_node in self.get_related_industry_tags(field["id"]):
                if industry_node not in industry_tags:
                    industry_tags.append(industry_node)

        logger.info("添加所有产业领域关系=[{}]".format(industry_tags))

        return industry_tags

    def process_channel_rel(self, properties):
        '''与渠道实体的关系添加'''
        channel_rel = []
        sql_conn = pymysql.connect( host = self.config.get("mysql","host") ,
                            user = self.config.get("mysql","user") ,
                            passwd = self.config.get("mysql","passwd"),
                            port = self.config.getint("mysql","port") ,
                            db = self.config.get("mysql","db"),
                            charset = "utf8" )
        sql_cur = sql_conn.cursor() 
        # 查询企业相关的渠道信息
        sql_state = self.config.get("mysql","company_channel_query").replace("eq","=").format(properties["name"])
        sql_cur.execute(sql_state)
        datas = sql_cur.fetchall()
        for data in datas:
            company_name, channel_name, channel_id = data
            rel = {
                "relation_type":"concept_relation/100010",
                "object_name":channel_name,
                "object_type": "channel",
                "object_id": channel_id,
            }
            channel_rel.append(rel)

        sql_cur.close()
        sql_conn.close()
        return channel_rel


        

    def process_relations(self, properties):
        '''
        手动更新全量公司所有产业领域分类
        '''
        relations = []

        industry_rel = self.process_industry_rel(properties)
        relations.extend(industry_rel)

        return relations

    def process_realtion_company(self, input_q):

        while input_q.empty() is not True:
            company = input_q.get()
            company_key = company["_key"]
            
            relations = [] 
            # 获取新的产业领域分类
            relations = self.process_relations(company["properties"])
            try:
                doc = self.kb_company[company_key]
                old_relations = doc["relations"]

                for relation in copy.deepcopy(old_relations):
                    for obeject_id in self.update_industries_id:
                        if object_id in relation["object_id"]:#发现该分类或其子分类
                            old_relations.remove(relation)
                # 更新合并新产业领域分类数据
                relations.extend(old_relations)              
                doc["relations"] = relations
                doc["update_time"] = datetime.datetime.today()
                doc.save()
                self.count_graph_update += 1

                if self.count_graph_update % 100 == 0 or self.count_graph_update == self.total:
                    logger.info('#'*60)
                    logger.info("前[{}]家企业关系添加完成,还剩[{}]在队列中".format(self.count_graph_update,input_q.qsize()))
                    logger.info('#'*60)
            except Exception as e:
                logger.error("企业关系添加失败，企业名=[{}]".format(company["name"]))
            input_q.task_done()


        
    
    def process(self, scan_date):

        process_companys = self.query_process_company(scan_date)

        count = 0

        # arango数据库企业信息处理
        input_q = Queue()
        for company in process_companys:

            logger.info("处理企业关系，企业名=[{}]".format(company["name"]))

            input_q.put(company)
            count += 1
        logger.info("清洗库共找到企业{}个进入队列#################################".format(count))
        for _ in range(4):
            thread = threading.Thread(target=self.process_realtion_company,args=(input_q,))
            thread.daemon = True  # 随主线程退出而退出
            thread.start()
        
        logger.info("日期[{}]清洗库共找到企业{}个，arango企业库更新关系{}个".format(
            self.process_date, self.total, self.count_graph_update))

if __name__=="__main__":

    # 最早日期 2019-06-03

    rel = RelationPipeline()
    if len(sys.argv) > 1:
        rel.process(sys.argv[1])
    else:
        rel.process("yesterday")
