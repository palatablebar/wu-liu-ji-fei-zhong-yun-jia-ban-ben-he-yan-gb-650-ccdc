# 运行手册 freightrate

## 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -s http://127.0.0.1:18209/api/health
```

宿主机端口 18209 映射容器 8000。SQLite 落在命名卷 `freightrate-data` 的 `/app/data/freightrate.db`。

## 启动后自动完成的事

1. 建表
2. 装载 `sample_data/rate_cards.csv`（运价集 v3，38 行）
3. 采信 `sample_data/waybills_dirty.csv`
4. 计算计费重与账单

页面首次打开即可看到 27 条运单、15 票计费、12 行隔离、应收 1521.04 元。

## 手工重跑

```bash
curl -s -X POST http://127.0.0.1:18209/api/waybills/import -F sample=waybills_dirty.csv
curl -s -X POST http://127.0.0.1:18209/api/weights
curl -s -X POST http://127.0.0.1:18209/api/charges
curl -s http://127.0.0.1:18209/api/reconcile
```

同一份输入重复导入命中同一个 run_id，行数与合计不变。

## 服务变更（加急降普通）

```bash
# 单条变更
curl -s -X POST http://127.0.0.1:18209/api/changes \
  -H 'Content-Type: application/json' \
  -d '{"change_id":"C-1002","waybill_no":"WB-1002","from_service":"standard","to_service":"economy"}'
# 批量：{"changes":[ ... ]}
# 同一条再发一次返回 duplicate=true，台账行数与金额不变（幂等）
curl -s http://127.0.0.1:18209/api/changes
curl -s http://127.0.0.1:18209/api/export/service-changes.csv
```

客户应收合计保持 1521.04 元不下调；实际成本按变更后服务重算，差额进降级损失。
M5 重新计费后成本账自动失效重算，也可手动 `POST /api/changes/recompute`。

## 运价文件校验

```bash
curl -s -X POST http://127.0.0.1:18209/api/rates/audit -F sample=rate_cards_overlap.csv
```

返回重叠清单；`/api/rates/import` 在校验不通过时返回 409，不会替换现有运价。

## 重建数据库

```bash
docker compose down -v --remove-orphans
docker compose up -d --build
```

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面 500，提示 RATE_MISSING | 采信时运价表还没装载 | 先打开运价手册页确认 38 行版本窗口 |
| /api/charges 返回 409 NO_WEIGHTS | 还没算计费重 | 先调 /api/weights |
| 导入后 rows_ok 为 0 | 全部行都被隔离 | 到运单页看原因码分布 |
| 运价装载 409 RATE_STALE | 提交的运价集版本低于现有 | 提高 CSV 里的 rate_set_version |
| 服务变更 422 CHANGE_WAYBILL_MISSING | 变更号指向的运单不在本 run | 核对运单号与 run 参数 |
| 服务变更 422 CHANGE_CARRIER_MISMATCH | 变更承运商与运单承运商不一致 | 以运单承运商为准重新提交 |
| 成本行出现 COST_UNRATEABLE | 实际服务在提货时刻无运价版本 | 补该服务运价后调 /api/changes/recompute |
