import { describe, expect, it } from 'vitest'
import { isV1CompatibilityOpen, resolveAssistantApiMode } from './assistantRelease'

describe('Assistant Web release controls', () => {
  const beforeExpiry = Date.parse('2026-08-07T00:00:00Z')
  const afterExpiry = Date.parse('2026-10-01T00:00:00Z')
  const compatibilityUntil = '2026-09-30T23:59:59Z'

  it('defaults to v2 and permits v1 only during the compatibility window', () => {
    expect(resolveAssistantApiMode(undefined, compatibilityUntil, beforeExpiry)).toBe('v2')
    expect(resolveAssistantApiMode('v1', compatibilityUntil, beforeExpiry)).toBe('v1')
    expect(isV1CompatibilityOpen(compatibilityUntil, beforeExpiry)).toBe(true)
  })

  it('fails closed to v2 when the compatibility window is invalid or expired', () => {
    expect(resolveAssistantApiMode('v1', compatibilityUntil, afterExpiry)).toBe('v2')
    expect(resolveAssistantApiMode('v1', 'not-a-date', beforeExpiry)).toBe('v2')
  })
})
