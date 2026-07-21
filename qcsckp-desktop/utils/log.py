# -*- coding: utf-8 -*-
"""
日志配置模块
提供统一的日志配置功能
直接导入 logger 使用即可
支持按日期或按4小时分割日志，防止单文件过大
"""

import os
import logging
from logging.handlers import TimedRotatingFileHandler
from config import LOGS_DIR


# 创建 logs 目录（如果不存在）
log_dir = LOGS_DIR
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# 日志文件名（不包含后缀，后缀由处理器自动添加）
log_file_base = os.path.join(log_dir, 'app')

# 配置日志格式
log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
date_format = '%Y-%m-%d %H:%M:%S'

# 创建日志记录器
logger = logging.getLogger('QianChuanPMCServices')
logger.setLevel(logging.INFO)

# 避免重复添加处理器
if not logger.handlers:
    # 使用 TimedRotatingFileHandler 实现按时间分割日志
    # when='H' 表示按小时，interval=4 表示每4小时分割一次
    # midnight 表示每天午夜分割
    # backupCount=30 表示保留30天的日志文件
    file_handler = TimedRotatingFileHandler(
        filename=log_file_base,
        when='H',           # 按小时分割
        interval=4,         # 每4小时分割一次
        backupCount=30,     # 保留30个备份文件
        encoding='utf-8',
        atTime=None         # 配合 when='H' 使用，每4小时自动分割
    )
    # 日志文件后缀格式（用于区分不同时间的日志文件）
    file_handler.suffix = '%Y%m%d-%H'  # 如：20260227-08 表示8点到12点的日志
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(log_format, date_format))

    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))

    # 添加处理器到日志记录器
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

logger.info(f'日志服务已启动，每4小时分割日志，保留30天')
