# 口述资料同意范围与撤回处置

服务以 Django REST Framework 和本地 SQLite 提供口述资料授权后端基础。

## 开发命令

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m unittest discover -s tests -v`
- 编译或构建检查：`python3 -m compileall -q project consent manage.py`
- 启动服务：`python3 manage.py runserver 0.0.0.0:8080`

测试和构建只使用仓库内数据，不需要连接外部业务服务。
