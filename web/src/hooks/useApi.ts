import { useCallback, useEffect, useState } from 'react'

interface State<T> {
  readonly data?: T
  readonly error?: Error
  readonly loading: boolean
}

/**
 * Runs a request and tracks its state.
 *
 * Every request gets an AbortSignal and every effect aborts on cleanup, so a fast sequence of
 * filter changes cannot land an old response over a newer one.
 */
export function useApi<T>(
  request: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): State<T> & { readonly reload: () => void } {
  const [state, setState] = useState<State<T>>({ loading: true })
  const [nonce, setNonce] = useState(0)

  // The caller passes a fresh closure each render; the dependency list is the real identity.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(request, deps)

  useEffect(() => {
    const controller = new AbortController()
    setState((previous) => ({ ...previous, loading: true, error: undefined }))

    run(controller.signal)
      .then((data) => setState({ data, loading: false }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setState({ error: error instanceof Error ? error : new Error(String(error)), loading: false })
      })

    return () => controller.abort()
  }, [run, nonce])

  return { ...state, reload: () => setNonce((value) => value + 1) }
}
