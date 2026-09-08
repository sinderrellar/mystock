// format.js —— 数字格式化 + 涨跌语义（A股：红涨绿跌）
export const isNum = (n) => n !== null && n !== undefined && n !== '' && !Number.isNaN(Number(n))

export const fmt = (n, d = 2) => (isNum(n) ? Number(n).toFixed(d) : '—')

export const money = (n, d = 2) => {
  if (!isNum(n)) return '—'
  const x = Number(n)
  const abs = Math.abs(x)
  if (abs >= 1e8) return (x / 1e8).toFixed(d) + ' 亿'
  if (abs >= 1e4) return (x / 1e4).toFixed(d) + ' 万'
  return x.toFixed(d)
}

export const pct = (n, d = 2) => (isNum(n) ? `${Number(n) > 0 ? '+' : ''}${Number(n).toFixed(d)}%` : '—')

// 涨/跌样式类：>0 红(num-up)，<0 绿(num-down)
export const signedCls = (n) => (isNum(n) && Number(n) > 0 ? 'num-up' : isNum(n) && Number(n) < 0 ? 'num-down' : '')

export const signedPct = (n, d = 2) => {
  if (!isNum(n)) return '—'
  const v = Number(n)
  return `${v > 0 ? '+' : ''}${v.toFixed(d)}%`
}

// 状态 → tag 颜色
export const tagCls = (s) => {
  const text = String(s || '')
  if (/涨|强势|领先|超配|乐观|达标|通过|满足|持有|看多|吸筹|多|好转|上升|盈利|高/.test(text)) return 'tag red'
  if (/跌|弱|滞后|低配|悲观|不达标|未满|触发|止损|卖出|减仓|派发|风险|亏损|恶化|下降|低|慎/.test(text)) return 'tag green'
  if (/震荡|分歧|中性|观望|回撤|警告|关注|复苏|修复/.test(text)) return 'tag orange'
  return 'tag blue'
}

export const statusText = (s) => String(s || '—').replace(/^[🔥❄️⚠️⚡✅🚀📉📈🔺🔻●▲▼◼]+/, '').trim() || '—'
