"""Independent CoreELEC service manager entry point."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'resources', 'lib'))
from manager import run
if __name__ == '__main__':
    run()
