#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : logger.py
@Description: 日志工具模块
              - 支持按日期自动切换日志文件
              - 每天在对应的日期文件中记录日志
              - 跨天时自动切换到新的日志文件
"""
import logging
import os
from datetime import datetime
from logging.handlers import BaseRotatingHandler


class DailyRotatingFileHandler(BaseRotatingHandler):
    """
    按日期轮转的文件处理器
    
    功能：
    - 每天自动切换到对应日期的日志文件
    - 格式：log/YYYY-MM-DD.log
    - 跨天时自动创建新文件并关闭旧文件
    """
    
    def __init__(self, log_dir: str, encoding: str = 'utf-8'):
        """
        初始化按日期轮转的文件处理器
        
        Args:
            log_dir: 日志目录路径
            encoding: 文件编码，默认为 utf-8
        """
        self.log_dir = log_dir
        self.encoding = encoding
        self.current_date = None
        self.current_file = None
        self.baseFilename = None
        
        # 确保日志目录存在
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        # 初始化当前日期的文件
        self._update_file()
        
        # 调用父类初始化（BaseRotatingHandler 需要 baseFilename）
        super().__init__(self.baseFilename, 'a', encoding=encoding, delay=False)
    
    def _get_log_filename(self, date: datetime) -> str:
        """
        根据日期生成日志文件名
        
        Args:
            date: 日期对象
            
        Returns:
            日志文件完整路径
        """
        date_str = date.strftime('%Y-%m-%d')
        return os.path.join(self.log_dir, f"{date_str}.log")
    
    def _update_file(self):
        """
        更新当前日志文件（如果日期变化则切换到新文件）
        """
        today = datetime.now().date()
        
        # 如果日期没有变化，不需要更新
        if self.current_date == today:
            return
        
        # 日期变化，需要切换文件
        old_file = self.current_file
        self.current_date = today
        self.current_file = self._get_log_filename(datetime.now())
        self.baseFilename = self.current_file
        
        # 如果之前有打开的文件，先关闭
        if old_file and old_file != self.current_file and self.stream:
            try:
                self.stream.close()
            except Exception:
                pass
        
        # 打开新文件
        if not os.path.exists(self.current_file):
            # 创建新文件
            with open(self.current_file, 'a', encoding=self.encoding):
                pass
    
    def _open(self):
        """
        打开当前日志文件
        
        Returns:
            文件对象
        """
        # 每次打开前检查日期是否变化
        self._update_file()
        return open(self.baseFilename, self.mode, encoding=self.encoding)
    
    def shouldRollover(self, record):
        """
        判断是否需要轮转（检查日期是否变化）
        
        Args:
            record: 日志记录对象
            
        Returns:
            如果需要轮转返回 True，否则返回 False
        """
        if self.current_date is None:
            return True
        
        today = datetime.now().date()
        return today != self.current_date
    
    def doRollover(self):
        """
        执行轮转操作（切换到新日期的日志文件）
        """
        if self.stream:
            try:
                self.stream.close()
            except Exception:
                pass
        
        # 更新到新日期的文件
        self._update_file()
        
        # 重新打开文件流
        self.stream = self._open()
    
    def emit(self, record):
        """
        发送日志记录（重写以确保日期检查）
        
        Args:
            record: 日志记录对象
        """
        # 在写入前检查日期是否变化
        if self.shouldRollover(record):
            self.doRollover()
        
        # 调用父类的 emit 方法
        super().emit(record)


def setup_logger(name="Bot"):
    """
    设置日志记录器
    
    功能：
    - 创建按日期轮转的文件处理器
    - 同时输出到文件和控制台
    - 支持跨天自动切换日志文件
    
    Args:
        name: 日志记录器名称，默认为 "Bot"
        
    Returns:
        配置好的日志记录器对象
    """
    log_dir = "log"
    
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    # 避免重复添加处理器
    if not logger.handlers:
        formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s',
            datefmt='%H:%M:%S'
        )
        
        # 文件输出（按日期轮转）
        fh = DailyRotatingFileHandler(log_dir, encoding='utf-8')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        
        # 控制台输出
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        logger.addHandler(ch)
    
    return logger


# 全局单例 logger
logger = setup_logger("SmartMoneyBot")
