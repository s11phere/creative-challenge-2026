export type AssistantApiMode = 'v1' | 'v2'

const DEFAULT_V1_COMPATIBILITY_UNTIL = '2026-09-30T23:59:59Z'

export function isV1CompatibilityOpen(
  compatibilityUntil: string | undefined,
  now = Date.now(),
): boolean {
  const expiresAt = Date.parse(compatibilityUntil ?? DEFAULT_V1_COMPATIBILITY_UNTIL)
  return Number.isFinite(expiresAt) && now <= expiresAt
}

export function resolveAssistantApiMode(
  requestedMode: string | undefined,
  compatibilityUntil: string | undefined,
  now = Date.now(),
): AssistantApiMode {
  return requestedMode === 'v1' && isV1CompatibilityOpen(compatibilityUntil, now) ? 'v1' : 'v2'
}

export const assistantV1CompatibilityUntil =
  import.meta.env.VITE_ASSISTANT_V1_COMPATIBILITY_UNTIL ?? DEFAULT_V1_COMPATIBILITY_UNTIL
export const assistantV1CompatibilityAvailable = isV1CompatibilityOpen(assistantV1CompatibilityUntil)
export const assistantDefaultApiMode = resolveAssistantApiMode(
  import.meta.env.VITE_ASSISTANT_DEFAULT_API_MODE,
  assistantV1CompatibilityUntil,
)
