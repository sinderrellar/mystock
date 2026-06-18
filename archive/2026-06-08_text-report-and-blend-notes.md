# 2026-06-08 文本报告完善 + 行业归一化标注 + 百分位后缀修复

## 1. 改了什么

**涉及文件：**
- `scripts/pyramid_multifactor_strategy.py` — `_format_text_report` 重写 + `_blend_score` 加诊断标注
- `scripts/portfolio_strategy.py` — `_pct_suffix` 从实例方法改为 classmethod（修复 NameError）

**具体变更：**

### 1.1 文本报告展示新数据（pyramid）

`_format_text_report` 格式化输出从 4 行/股扩展到完整展示所有新维度：

- **数据质量**：显示百分比 + 不全时标 `⚠`
- **估值**：PE/PB 含行业百分位 + 股息率
- **质量**：ROE/毛利率含行业百分位 + 负债率 + OCF/NI（✓/⚠标记）+ 数据新鲜度
- **成长**：营收/利润增长 + 新鲜度
- **动量**：1月/3月收益 + 路径效率/最大回撤/趋势乘数
- **因子冲突**：价值-质量分歧诊断，带 `⚠️`/`🚨` 标记
- **行业归一化标注**：汇总 value/quality 的 `blend_notes`

### 1.2 行业归一化诊断标注（pyramid）

`_blend_score` 在三种情况下向 `details['blend_notes']` 写入说明：

- **无行业代码**：`"pe: 无行业代码，未做行业归一化"`
- **行业内样本不足**：`"pe: 行业内样本不足(<5只)，未做行业归一化"`
- **行业缺该指标分布数据**：`"pe: 行业缺少分布数据，未做行业归一化"`
- **估值在行业分布范围外**（PE≤0/PE≥500/PB≤0/PB≥100）：`"pe(550.0): 值在行业分布范围[5.2,485.3]之外，该股票可能未参与行业百分位计算（估值异常/亏损/极端）"`

### 1.3 百分位后缀 NameError 修复（portfolio_strategy）

`format_review` 是独立函数（非类方法），调用 `self._pct_suffix(code)` 会 NameError。

**修复**：`_load_percentile_ranks` 和 `_pct_suffix` 从实例方法改为 `@classmethod`，缓存存在类变量 `_PCT_CACHE` 上。`format_review` 中改为 `PortfolioStrategy._pct_suffix(code)`。

## 2. 为什么改

- 命令行跑 `python3 scripts/pyramid_multifactor_strategy.py` 时看不到行业百分位、路径质量、因子冲突等新数据，JSON 里有但文本报告没有
- 行业归一化在股票被排除出分布时（亏损/极端估值）无任何提示，用户不知道某只股票的估值分是纯绝对分还是做了行业混合
- `format_review` NameError 导致 portfolio review 无法运行

## 3. 怎么验证

运行 `python3 scripts/portfolio_strategy.py review`：不再报 NameError，因子综合得分后显示百分位后缀

运行 `python3 scripts/pyramid_multifactor_strategy.py`：文本报告输出包含估值百分位、质量现金流/新鲜度、动量路径质量、因子冲突诊断
