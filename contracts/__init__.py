"""contracts — 全链路阶段产物契约（promoted from experiments/artifact-contracts/）。

- models.py   : 全部 artifact pydantic 定义（"schema": "<name>/<v>"）+ JSON Schema 发射
- validate.py : 跨字段校验器（schema lint + id 引用 + coverage + manifest sha256）
- schemas/    : 发射出的 JSON Schema（供 TS/Remotion 侧消费）
"""
