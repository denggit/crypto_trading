#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/1/26
@File       : monitor_bots.py
@Description: Bot进程监控守护程序 - 自动检测并重启挂掉的进程
"""
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import psutil


@dataclass
class BotConfig:
    """
    Bot配置数据类
    
    Attributes:
        name: Bot名称
        project_dir: 项目目录路径
        main_script: 主脚本路径（相对于项目目录）
        log_file: 日志输出文件
        check_interval: 检查间隔（秒）
    """
    name: str
    project_dir: str
    main_script: str
    log_file: str
    check_interval: int = 30


class ProcessChecker:
    """
    进程检查器 - 负责检查进程是否在运行
    
    使用策略模式，便于后续扩展不同的检查方式
    """

    @staticmethod
    def is_process_running(script_path: str) -> bool:
        """
        检查指定脚本的进程是否在运行
        
        Args:
            script_path: 脚本的完整路径
            
        Returns:
            bool: 如果进程在运行返回True，否则返回False
        """
        try:
            script_name = Path(script_path).name
            script_dir = str(Path(script_path).parent)

            # 遍历所有Python进程
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
                try:
                    # 检查是否是Python进程
                    if proc.info['name'] and 'python' in proc.info['name'].lower():
                        cmdline = proc.info['cmdline']
                        if cmdline:
                            # 检查命令行参数中是否包含目标脚本
                            cmdline_str = ' '.join(cmdline)
                            if script_name in cmdline_str:
                                # 检查工作目录是否匹配
                                cwd = proc.info.get('cwd', '')
                                if script_dir in cwd or script_path in cmdline_str:
                                    # 验证进程确实在运行
                                    if proc.is_running():
                                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    # 进程可能已经结束或没有权限访问，继续检查下一个
                    continue

            return False
        except Exception as e:
            logging.warning(f"检查进程时出错: {e}")
            return False


class ProcessStarter:
    """
    进程启动器 - 负责启动bot进程
    
    使用策略模式，便于后续扩展不同的启动方式
    """

    @staticmethod
    def start_bot(config: BotConfig) -> Tuple[bool, Optional[str]]:
        """
        启动bot进程
        
        Args:
            config: Bot配置对象
            
        Returns:
            Tuple[bool, Optional[str]]: (是否成功, 错误信息)
        """
        try:
            project_path = Path(config.project_dir)
            if not project_path.exists():
                return False, f"项目目录不存在: {config.project_dir}"

            main_script_path = project_path / config.main_script
            if not main_script_path.exists():
                return False, f"主脚本不存在: {main_script_path}"

            log_file_path = project_path / config.log_file

            # 使用绝对路径启动进程，方便在ps命令中区分不同的bot
            # 切换到项目目录并启动进程
            main_script_abs_path = str(main_script_path.resolve())  # 确保使用绝对路径
            cmd = [
                'nohup',
                'python',
                main_script_abs_path,
                '>',
                str(log_file_path),
                '2>&1',
                '&'
            ]

            # 使用shell=True来支持重定向和后台运行
            subprocess.Popen(
                ' '.join(cmd),
                shell=True,
                cwd=str(project_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # 等待一小段时间，确认进程启动
            time.sleep(2)

            # 验证进程是否真的启动了
            if ProcessChecker.is_process_running(str(main_script_path)):
                return True, None
            else:
                return False, "进程启动后验证失败"

        except Exception as e:
            return False, f"启动进程时发生异常: {str(e)}"


class BotMonitor:
    """
    Bot监控器 - 核心监控逻辑
    
    使用观察者模式和策略模式，确保模块解耦
    """

    def __init__(self, bots: List[BotConfig], check_interval: int = 30):
        """
        初始化监控器
        
        Args:
            bots: Bot配置列表
            check_interval: 检查间隔（秒）
        """
        self.bots = bots
        self.check_interval = check_interval
        self.process_checker = ProcessChecker()
        self.process_starter = ProcessStarter()
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        """
        设置日志记录器
        
        Returns:
            logging.Logger: 配置好的日志记录器
        """
        logger = logging.getLogger('BotMonitor')
        logger.setLevel(logging.INFO)

        # 创建日志目录
        log_dir = Path(__file__).parent / 'log'
        log_dir.mkdir(exist_ok=True)

        # 文件处理器
        log_file = log_dir / f"monitor_{datetime.now().strftime('%Y-%m-%d')}.log"
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.INFO)

        # 控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)

        # 格式化器
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        return logger

    def check_bot(self, config: BotConfig) -> bool:
        """
        检查单个bot进程状态
        
        Args:
            config: Bot配置对象
            
        Returns:
            bool: 如果进程在运行返回True，否则返回False
        """
        main_script_path = Path(config.project_dir) / config.main_script
        return self.process_checker.is_process_running(str(main_script_path))

    def restart_bot(self, config: BotConfig) -> bool:
        """
        重启bot进程
        
        Args:
            config: Bot配置对象
            
        Returns:
            bool: 如果重启成功返回True，否则返回False
        """
        self.logger.warning(f"🔄 [{config.name}] 检测到进程挂掉，正在重启...")
        success, error_msg = self.process_starter.start_bot(config)

        if success:
            self.logger.info(f"✅ [{config.name}] 进程重启成功")
            return True
        else:
            self.logger.error(f"❌ [{config.name}] 进程重启失败: {error_msg}")
            return False

    def monitor_once(self) -> None:
        """
        执行一次监控检查
        
        如果发现多个bot挂掉，重启时会间隔60秒，避免同时启动造成资源竞争
        """
        # 先收集所有需要重启的bot
        bots_to_restart = []
        for bot_config in self.bots:
            if not self.check_bot(bot_config):
                bots_to_restart.append(bot_config)
            else:
                self.logger.debug(f"✓ [{bot_config.name}] 进程运行正常")

        # 逐个重启，每个之间间隔60秒
        for idx, bot_config in enumerate(bots_to_restart):
            if idx > 0:
                # 不是第一个需要重启的bot，等待60秒
                self.logger.info(f"⏳ 等待60秒后重启下一个bot...")
                time.sleep(60)
            self.restart_bot(bot_config)

    def run(self) -> None:
        """
        运行监控循环
        """
        self.logger.info("=" * 60)
        self.logger.info("🤖 Bot监控守护程序启动")
        self.logger.info(f"📋 监控目标: {len(self.bots)} 个bot")
        for bot in self.bots:
            self.logger.info(f"   - {bot.name}: {bot.project_dir}")
        self.logger.info(f"⏱️  检查间隔: {self.check_interval} 秒")
        self.logger.info("=" * 60)

        try:
            while True:
                self.monitor_once()
                time.sleep(self.check_interval)
        except KeyboardInterrupt:
            self.logger.info("🛑 收到停止信号，监控程序退出")
        except Exception as e:
            self.logger.error(f"💥 监控程序发生异常: {e}", exc_info=True)
            raise


def create_bot_configs() -> List[BotConfig]:
    """
    创建bot配置列表
    
    Returns:
        List[BotConfig]: Bot配置列表
    """
    base_path = "/root/project"

    bots = [
        BotConfig(
            name="botA_tugou",
            project_dir=f"{base_path}/botA_tugou",
            main_script="main.py",
            log_file="B.out"
        ),
        BotConfig(
            name="botB_stable",
            project_dir=f"{base_path}/botB_stable",
            main_script="main.py",
            log_file="B.out"
        ),
        BotConfig(
            name="botC_diamond",
            project_dir=f"{base_path}/botC_diamond",
            main_script="main.py",
            log_file="B.out"
        ),
    ]

    return bots


def main():
    """
    主函数 - 程序入口
    """
    # 创建bot配置
    bots = create_bot_configs()

    # 创建监控器并运行
    monitor = BotMonitor(bots, check_interval=30)
    monitor.run()


if __name__ == "__main__":
    main()
