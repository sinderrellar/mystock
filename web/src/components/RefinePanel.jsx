import React, { useState } from 'react'

export default function RefinePanel({ onRefine, refining }) {
  const [instruction, setInstruction] = useState('')

  function handleSubmit(e) {
    e.preventDefault()
    if (!instruction.trim()) return
    onRefine(instruction.trim())
    setInstruction('')
  }

  return (
    <form className="refine-panel" onSubmit={handleSubmit}>
      <input
        value={instruction}
        onChange={(e) => setInstruction(e.target.value)}
        placeholder="例如：把风险提示提到最上面 / 精简摘要到一句话 / 突出成长派观点"
        disabled={refining}
      />
      <button type="submit" className="btn secondary" disabled={refining || !instruction.trim()}>
        {refining ? '调整中…' : '微调页面'}
      </button>
    </form>
  )
}
