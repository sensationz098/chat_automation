"""
run_qa_benchmark.py — Main runner that executes the 100 Extremely Hard V2 Questions
from questions/run_qa_benchmark_v2.py and logs answers to answers/q_a_v2.txt.
"""

import sys
import os
import asyncio

# Add questions directory to path
sys.path.append(os.path.join(os.path.dirname(__file__), "questions"))

from run_qa_benchmark_v2 import run_benchmark

if __name__ == "__main__":
    asyncio.run(run_benchmark())
