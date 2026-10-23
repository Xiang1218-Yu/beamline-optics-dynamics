# 束线光学与截获事件模拟器

修复 `environment/beamline_optics` 中的程序。命令行接口为：

```bash
python3 -m beamline_optics --input INPUT.json --output OUTPUT.json --ledger LEDGER.json
```

程序模拟单一横向平面坐标系中的**线性束流光学**。纵向坐标为 `s`，两个横向坐标为 `x`、`y`；粒子状态中的 `slope` 是无量纲的小角度近似 `[x', y']`。输入 JSON 必须是对象，包含以下字段：

- `run_id`：非空字符串。本次批次的唯一标识。
- `line_end`：正有限数，束线终点的 `s` 坐标。
- `particles`：非空数组。每项有唯一非空字符串 `id`、正有限数 `rigidity`、非零有限数 `charge`、长度为 2 的有限数 `position` 和 `slope`。`position` 是 `[x,y]`，所有粒子从 `s=0` 出发。
- `elements`：数组，可为空。每项有唯一非空字符串 `id`、`type`、`s_start`、`s_end`。区间必须满足 `0 <= s_start < s_end <= line_end`，不同元件的有效区间不得重叠。`type` 只能是 `drift`、`quadrupole` 或 `collimator`。四极铁还必须给出有限数 `gradient`；准直器还必须给出正有限数 `radius`。
- `screens`：数组，可为空。每项有唯一非空字符串 `id` 和 `s`，满足 `0 < s <= line_end`。屏幕位置必须互不相同。屏幕是截获式诊断屏：粒子抵达屏幕后记录命中并停止继续传输。

未被元件覆盖的区间按漂移段处理。所有位置相同的边界先完成左侧区间的传输；若粒子恰好在准直器出口达到孔径边界，它先被准直器吸收，不能再被同一 `s` 处的屏幕记录。

## 物理传输规则

在任意长度为 `L` 的漂移段中，对每一个横向平面使用

```text
u_out = u + L * u'
u'_out = u'
```

其中 `u` 可以是 `x` 或 `y`。

在有限长四极铁中，先计算 `k = charge * gradient / rigidity`。`x` 平面使用 `K=k`，`y` 平面使用 `K=-k`。对每个平面独立应用传输：

```text
K > 0:  u_out = cos(sqrt(K)L) u + sin(sqrt(K)L)/sqrt(K) u'
        u'_out = -sqrt(K) sin(sqrt(K)L) u + cos(sqrt(K)L) u'
K = 0:  按漂移段处理
K < 0:  令 r=sqrt(-K)
        u_out = cosh(rL) u + sinh(rL)/r u'
        u'_out = r sinh(rL) u + cosh(rL) u'
```

因此，一个横向平面聚焦时，另一个平面必然散焦；改变电荷或梯度符号会交换两个平面的响应。任何产生非有限中间状态或输出状态的计算都必须作为输入/数值错误拒收，且不能修改已有输出或账本。

## 准直器与屏幕事件

准直器自身没有聚焦场，其内部按漂移传播，但代表有限长度的圆孔管道。进入准直器时若 `x²+y² >= radius²`，粒子立刻在 `s_start` 被吸收。否则必须求出在该段内**首次**满足 `x(s)²+y(s)² = radius²` 的位置；不能只检查段末位置。发生吸收时停止在交点，向 `losses` 写入：

```json
{"id":"...","element_id":"...","s":...,"position":[x,y]}
```

同一位置的损失按 `id` 字典序排列，所有损失按 `(s, id)` 排列。存活粒子抵达屏幕后写入：

```json
{"id":"...","screen_id":"...","s":...,"position":[x,y],"slope":[x_prime,y_prime]}
```

屏幕命中按 `(s, id)` 排列。屏幕命中的粒子不会出现在最终 `particles` 中。最终透射粒子传输至 `line_end`，按 `id` 字典序输出，并包含 `s: line_end`。

## 诊断、账本与写入语义

成功输出必须是 UTF-8 JSON，键集合固定为 `status`、`run_id`、`line_end`、`particles`、`losses`、`hits`、`diagnostics`、`ledger_totals`。`status` 为 `"ok"`。

`diagnostics` 必须包含：

- `transmitted_count`、`absorbed_count`、`screened_count`；
- `transmitted_charge`，仅统计最终透射粒子的电荷；
- `centroid`：最终透射粒子的 `[mean(x), mean(x'), mean(y), mean(y')]`；没有透射粒子时为 `[0.0,0.0,0.0,0.0]`；
- `covariance`：最终透射粒子的 4×4 总体协方差矩阵，变量顺序与 `centroid` 相同；少于两个透射粒子时为全零矩阵；
- `rms_emittance`：对象 `{ "x": ..., "y": ... }`，分别为 `sqrt(max(0, cov(x,x)*cov(x',x')-cov(x,x')²))` 和 y 平面的对应值。

`--ledger` 指向可持久化的物理批次账本。它是公开持久化接口，不是实现自定义格式。账本 JSON 的**键集合必须恰好**为 `schema`、`run_ids`、`totals`，并使用下列格式：

```json
{
  "schema": "beamline-optics-ledger/v1",
  "run_ids": ["batch-001", "batch-002"],
  "totals": {
    "runs": 0,
    "transmitted": 0,
    "absorbed": 0,
    "screened": 0,
    "transmitted_charge": 0.0
  }
}
```

文件不存在时从上面的空账本开始（`run_ids` 为空数组，所有计数为 `0`，`transmitted_charge` 为有限数 `0.0`）。已有账本必须满足该精确结构：`schema` 必须为字符串 `"beamline-optics-ledger/v1"`；`run_ids` 必须是互不重复的非空字符串数组；`totals` 的键集合必须恰好为上述五项，其中前四项为非负整数，`transmitted_charge` 为有限数，并且 `totals.runs` 必须恰好等于 `run_ids` 的长度。不得接受额外键、缺失键或其他架构版本。

每次成功运行必须把本次唯一的 `run_id` 追加到 `run_ids` 的末尾，并更新累计 `runs`、`transmitted`、`absorbed`、`screened` 和 `transmitted_charge`。重复 `run_id` 必须拒收。输出中的 `ledger_totals` 必须等于本次提交后的 `totals` 对象。

结构错误、非法范围、非有限数、重叠元件、重复粒子或屏幕标识、屏幕位置冲突、损坏账本、重复 `run_id` 或数值溢出都必须拒收：退出码非零、标准错误以 `INPUT_ERROR:` 开头，已有 `--output` 与 `--ledger` 文件均逐字节不变，且对应目录中不能遗留临时文件。成功写入时，输出和账本各自必须采用同目录临时文件加原子替换。

## Requirement identifiers

- **R1**：在显式元件与未覆盖间隙中执行正确的漂移传输。
- **R2**：依照电荷、刚度和梯度计算有限长四极铁的两个横向平面传输。
- **R3**：正确组合相邻传输矩阵，并在元件边界改变场规则。
- **R4**：在有限长度准直器内判定入口损失和首次圆孔交点。
- **R5**：执行准直器损失与截获屏之间规定的事件优先级和终止语义。
- **R6**：在元件内部的事件平面上做精确部分传输，并稳定排序同位事件。
- **R7**：只用最终透射粒子计算束流诊断、协方差和 RMS 发射度。
- **R8**：以唯一批次标识维护跨运行的物理账本累计值。
- **R9**：对所有声明的非法输入、账本和数值错误拒收，且不产生可见副作用。
- **R10**：通过同目录临时文件与原子替换提交输出和账本，处理数值溢出与既有路径。
