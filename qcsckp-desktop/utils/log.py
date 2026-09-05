# -*- coding: utf-8 -*-
"""
日志配置模块
提供统一的日志配置功能
直接导入 logger 使用即可
支持按日期或按4小时分割日志，防止单文件过大
"""

import os
import logging
import re
import threading
from logging.handlers import TimedRotatingFileHandler
from config import LOGS_DIR
from utils.log_redaction import redact_text


# Directory creation belongs to configure_logging so handler setup is atomic.
log_dir = LOGS_DIR

# 日志文件名（不包含后缀，后缀由处理器自动添加）
log_file_base = os.path.join(log_dir, 'app')

# 配置日志格式
log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
date_format = '%Y-%m-%d %H:%M:%S'

# 创建日志记录器
logger = logging.getLogger('QianChuanPMCServices')
logger.setLevel(logging.INFO)

_CONFIG_LOCK = threading.RLock()


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Redact rendered arguments and traceback text without changing the
        # LogRecord shared with unrelated handlers.
        return redact_text(super().format(record))


def configure_logging(logs_dir: str, target: logging.Logger) -> logging.Logger:
    """Ensure a disk sink even if another library already added a handler."""
    with _CONFIG_LOCK:
        os.makedirs(logs_dir, exist_ok=True)
        filename = os.path.normcase(os.path.abspath(os.path.join(logs_dir, 'app')))
        target.setLevel(logging.INFO)
        target.disabled = False
        matching = [
            handler for handler in target.handlers
            if isinstance(handler, TimedRotatingFileHandler)
            and getattr(handler, '_qcsckp_disk_sink', False)
            and os.path.normcase(handler.baseFilename) == filename
        ]
        if not matching:
            handler = TimedRotatingFileHandler(
                filename=filename, when='H', interval=4, backupCount=30,
                encoding='utf-8',
            )
            handler.suffix = '%Y%m%d-%H'
            handler._qcsckp_disk_sink = True
            target.addHandler(handler)
            matching.append(handler)
        formatter = RedactingFormatter(log_format, date_format)
        for handler in matching:
            # Rotation discovery must match our historical compact filenames;
            # changing suffix alone leaves the default YYYY-MM-DD_HH matcher.
            handler.extMatch = re.compile(r'\d{8}-\d{2}', re.ASCII)
            handler.setLevel(logging.INFO)
            handler.setFormatter(formatter)
        if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
                   for h in target.handlers):
            console = logging.StreamHandler()
            console.setLevel(logging.INFO)
            console.setFormatter(formatter)
            target.addHandler(console)
        return target


configure_logging(log_dir, logger)

logger.info('日志服务已启动，每4小时分割日志，保留30个轮转文件')
