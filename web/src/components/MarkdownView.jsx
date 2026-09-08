import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// 研究报告 markdown 渲染：react-markdown + remark-gfm（表格必需）。
// react-markdown 默认转义 HTML（不用 dangerouslySetInnerHTML），安全。
export default function MarkdownView({ content }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content || ''}</ReactMarkdown>
    </div>
  )
}
