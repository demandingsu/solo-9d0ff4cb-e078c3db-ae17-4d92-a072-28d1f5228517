"""gunicorn / python 启动入口。

gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 wsgi:application
（案例状态在进程内存中，因此 compose 固定单 worker。）
"""

import os

from app import create_app

application = create_app()
app = application

if __name__ == "__main__":
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
