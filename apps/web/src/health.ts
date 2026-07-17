export type DependencyCheck = {
  healthy: boolean
  code: string
}

export type ReadyResponse = {
  status: 'ready' | 'degraded'
  checks: {
    postgresql: DependencyCheck
    redis: DependencyCheck
    model: DependencyCheck
  }
}

export type HealthSnapshot = {
  ready: ReadyResponse
  traceId: string | null
  requestId: string | null
  checkedAt: number
}

export class HealthApiError extends Error {
  readonly code: 'API_TIMEOUT' | 'API_UNREACHABLE' | 'INVALID_RESPONSE'

  constructor(code: 'API_TIMEOUT' | 'API_UNREACHABLE' | 'INVALID_RESPONSE') {
    super(code)
    this.code = code
  }
}

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

function isDependencyCheck(value: unknown): value is DependencyCheck {
  if (typeof value !== 'object' || value === null) return false
  const check = value as Record<string, unknown>
  return typeof check.healthy === 'boolean' && typeof check.code === 'string'
}

function isReadyResponse(value: unknown): value is ReadyResponse {
  if (typeof value !== 'object' || value === null) return false
  const response = value as Record<string, unknown>
  if (response.status !== 'ready' && response.status !== 'degraded') return false
  if (typeof response.checks !== 'object' || response.checks === null) return false
  const checks = response.checks as Record<string, unknown>
  return (
    isDependencyCheck(checks.postgresql) &&
    isDependencyCheck(checks.redis) &&
    isDependencyCheck(checks.model)
  )
}

async function parseJson(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    throw new HealthApiError('INVALID_RESPONSE')
  }
}

export async function fetchHealthSnapshot(parentSignal?: AbortSignal): Promise<HealthSnapshot> {
  const controller = new AbortController()
  let timedOut = false
  const timeout = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, 8_000)
  const abortFromParent = () => controller.abort()
  parentSignal?.addEventListener('abort', abortFromParent, { once: true })

  try {
    const [liveResponse, readyResponse] = await Promise.all([
      fetch(`${apiBaseUrl}/api/v1/health/live`, {
        headers: { Accept: 'application/json' },
        signal: controller.signal,
      }),
      fetch(`${apiBaseUrl}/api/v1/health/ready`, {
        headers: { Accept: 'application/json' },
        signal: controller.signal,
      }),
    ])
    if (!liveResponse.ok || ![200, 503].includes(readyResponse.status)) {
      throw new HealthApiError('API_UNREACHABLE')
    }

    const live = await parseJson(liveResponse)
    const ready = await parseJson(readyResponse)
    if (
      typeof live !== 'object' ||
      live === null ||
      (live as Record<string, unknown>).status !== 'alive' ||
      !isReadyResponse(ready)
    ) {
      throw new HealthApiError('INVALID_RESPONSE')
    }

    return {
      ready,
      traceId: readyResponse.headers.get('x-trace-id'),
      requestId: readyResponse.headers.get('x-request-id'),
      checkedAt: Date.now(),
    }
  } catch (error) {
    if (error instanceof HealthApiError) throw error
    throw new HealthApiError(timedOut ? 'API_TIMEOUT' : 'API_UNREACHABLE')
  } finally {
    window.clearTimeout(timeout)
    parentSignal?.removeEventListener('abort', abortFromParent)
  }
}

export function healthApiLabel(): string {
  return apiBaseUrl || '同源 /api/v1'
}
