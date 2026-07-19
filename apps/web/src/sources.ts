/** API client for sources, ingestion, and tasks. */

import { healthApiLabel } from './health'

export type SourceInfo = {
  id: string
  space_id: string
  source_type: string
  uri: string
  created_at: string
}

export type DocumentInfo = {
  id: string
  stable_key: string
  current_version_id: string | null
  status: string
  created_at: string
}

export type TaskInfo = {
  task_id: string
  source_id: string
  operation: string
  status: string
  stage: string
  progress: number
  retry_count: number
  max_retries: number
  error_code: string | null
  error: string | null
  created_at: string
}

export type SourceList = {
  sources: SourceInfo[]
}

export type SourceDetail = {
  source: SourceInfo
  documents: DocumentInfo[]
}

export type UploadResult = {
  source_id: string
  document_id: string
  blob_hash: string
  is_new_document: boolean
  is_unchanged: boolean
  task_id: string | null
}

export type IngestResult = {
  task_id: string
}

export class SourcesApiError extends Error {
  code: string
  status: number

  constructor(message: string, code: string, status: number) {
    super(message)
    this.name = 'SourcesApiError'
    this.code = code
    this.status = status
  }
}

const SPACE_ID = '00000000-0000-0000-0000-000000000000' // default space for MVP

async function apiFetch<T>(
  path: string,
  options?: { method?: string; body?: FormData | object },
): Promise<T> {
  const base = healthApiLabel()
  const url = `${base}${path}`

  const headers: Record<string, string> = {}
  let body: BodyInit | undefined

  if (options?.body instanceof FormData) {
    body = options.body
  } else if (options?.body) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  const response = await fetch(url, {
    method: options?.method ?? 'GET',
    headers,
    body,
  })

  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }))
    throw new SourcesApiError(
      detail.detail ?? response.statusText,
      `HTTP_${response.status}`,
      response.status,
    )
  }

  return response.json() as Promise<T>
}

/** List all sources. */
export function fetchSources(_signal?: AbortSignal): Promise<SourceList> {
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources`, { method: 'GET' })
}

/** Get source detail with documents. */
export function fetchSourceDetail(sourceId: string, _signal?: AbortSignal): Promise<SourceDetail> {
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources/${sourceId}/detail`, { method: 'GET' })
}

/** Upload a file to a source and trigger ingestion. */
export function uploadFile(sourceId: string, file: File): Promise<UploadResult> {
  const formData = new FormData()
  formData.append('file', file)
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources/${sourceId}/upload`, {
    method: 'POST',
    body: formData,
  })
}

/** Trigger ingestion for a source. */
export function triggerIngestion(sourceId: string): Promise<IngestResult> {
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources/${sourceId}/ingest`, {
    method: 'POST',
    body: {},
  })
}

/** Get task status. */
export function fetchTaskStatus(taskId: string, _signal?: AbortSignal): Promise<TaskInfo> {
  return apiFetch(`/api/v1/tasks/${taskId}`, { method: 'GET' })
}

/** Cancel a running task. */
export function cancelTask(taskId: string): Promise<TaskInfo> {
  return apiFetch(`/api/v1/tasks/${taskId}/cancel`, { method: 'POST', body: {} })
}

/** Retry a failed task. */
export function retryTask(taskId: string): Promise<IngestResult> {
  return apiFetch(`/api/v1/tasks/${taskId}/retry`, { method: 'POST', body: {} })
}
