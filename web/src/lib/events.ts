import type { QueryClient } from '@tanstack/react-query'
import { create } from 'zustand'
import { withToken } from './api'

type EventState = {
  connected: boolean
  last?: { name: string; data: unknown }
}

export const useEventStore = create<EventState>()(() => ({ connected: false }))

const EVENT_NAMES = ['hello', 'device', 'job', 'progress', 'log', 'toast'] as const

export function startEventStream(qc: QueryClient): () => void {
  const es = new EventSource(withToken('/api/events'))

  for (const name of EVENT_NAMES) {
    es.addEventListener(name, (e) => {
      let data: unknown = null
      try {
        data = JSON.parse((e as MessageEvent<string>).data)
      } catch {
        // keep going with null data; one bad frame should not stop the stream
      }
      useEventStore.setState(
        name === 'hello' ? { connected: true, last: { name, data } } : { last: { name, data } },
      )
      // "hello" also arrives after a reconnect, so a missed device event heals at once.
      if (name === 'hello' || name === 'device' || name === 'job') {
        void qc.invalidateQueries({ queryKey: ['status'] })
      }
    })
  }

  // EventSource reconnects by itself; "hello" marks us connected again.
  es.onerror = () => useEventStore.setState({ connected: false })

  return () => es.close()
}
