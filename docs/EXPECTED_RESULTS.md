# 规范结果（fixtures/plan.json + fixtures/rows.json）

期望值由 tests/verify.py 中独立 oracle 直接扫描原始记录生成；
NULL 表示 JSON null；avg_amount 为最简分数 `分子/分母`，分母为零（无是非空金额）时为 NULL。

| set_id | grouping_id | region | channel | count_star | count_amount | sum_amount | avg_amount |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 0 | NULL | web | 2 | 2 | 50 | 25/1 |
| 0 | 0 | east | NULL | 1 | 0 | NULL | NULL |
| 0 | 0 | east | web | 1 | 1 | 10 | 10/1 |
| 0 | 0 | west | store | 1 | 0 | NULL | NULL |
| 1 | 1 | NULL | NULL | 2 | 2 | 50 | 25/1 |
| 1 | 1 | east | NULL | 2 | 1 | 10 | 10/1 |
| 1 | 1 | west | NULL | 1 | 0 | NULL | NULL |
| 2 | 3 | NULL | NULL | 5 | 3 | 60 | 20/1 |
| 3 | 1 | NULL | NULL | 2 | 2 | 50 | 25/1 |
| 3 | 1 | east | NULL | 2 | 1 | 10 | 10/1 |
| 3 | 1 | west | NULL | 1 | 0 | NULL | NULL |

共 11 行；一次处理、四块处理、打乱合并顺序的管道输出必须与本表逐项一致。
