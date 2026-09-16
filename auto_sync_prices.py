#!/usr/bin/env python3
"""zsage 每日价格自动同步脚本

每天北京时间 8:00 自动运行 zsage sync-prices --auto
用于定时更新模型价格数据。

使用方法：
1. Windows: 通过任务计划程序创建定时任务
2. Linux/macOS: 通过 cron 创建定时任务

手动测试：
  python auto_sync_prices.py
"""

import sys
import subprocess
from pathlib import Path

def main():
    # 获取 zsage.py 的路径
    script_dir = Path(__file__).resolve().parent
    zsage_path = script_dir / "zsage.py"
    
    if not zsage_path.exists():
        print(f"错误：找不到 zsage.py 在 {zsage_path}", file=sys.stderr)
        return 1
    
    # 调用 zsage sync-prices --auto
    try:
        result = subprocess.run(
            [sys.executable, str(zsage_path), "sync-prices", "--auto"],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        # 输出结果
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        
        return result.returncode
        
    except subprocess.TimeoutExpired:
        print("错误：同步超时（60秒）", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
