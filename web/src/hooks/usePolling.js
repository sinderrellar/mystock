import { useEffect, useRef, useState } from 'react'

// 轮询 hook：setTimeout 递归，终止条件由 shouldStop(value) 决定，组件卸载自动清理。
export default function usePolling(fn, { interval = 3000, enabled = true, shouldStop } = {}) {
  const [value, setValue] = useState(null)
  const [error, setError] = useState(null)
  const fnRef = useRef(fn)
  fnRef.current = fn
  const stopRef = useRef(shouldStop)
  stopRef.current = shouldStop

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    let timer = null
    const tick = async () => {
      try {
        const v = await fnRef.current()
        if (cancelled) return
        setValue(v)
        setError(null)
        if (stopRef.current && stopRef.current(v)) return
      } catch (e) {
        if (!cancelled) setError(e.message)
      }
      if (!cancelled) timer = setTimeout(tick, interval)
    }
    tick()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [enabled, interval])

  return { value, error }
}
