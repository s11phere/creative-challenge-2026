/** API client for sources, ingestion, and tasks. */

import { apiBaseUrl } from './health'

export type SourceInfo = {
  id: string
  space_id: string
  source_type: string
  uri: string
  name: string
  created_at: string
  doc_count: number
  available_count: number
  failed_count: number
  primary_document_name: string | null
}

export type DocumentInfo = {
  id: string
  stable_key: string
  display_name: string
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

export type CreateSourceResult = {
  source_id: string
  space_id: string
  source_type: string
  uri: string
  name: string
  is_new: boolean
}

export type IngestResult = {
  task_id: string
}

export type IngestBatchResult = {
  task_ids: string[]
}

export type UploadLimits = {
  max_upload_size_mb: number
  max_upload_size_bytes: number
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
  options?: { method?: string; body?: FormData | object; signal?: AbortSignal },
): Promise<T> {
  const url = `${apiBaseUrl}${path}`

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
    signal: options?.signal,
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
export function fetchSources(signal?: AbortSignal): Promise<SourceList> {
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources`, { method: 'GET', signal })
}

/** Upload limits (max file size) from the live API configuration. */
export function fetchUploadLimits(signal?: AbortSignal): Promise<UploadLimits> {
  return apiFetch(`/api/v1/config/limits`, { method: 'GET', signal })
}

/** Get source detail with documents. */
export function fetchSourceDetail(sourceId: string, signal?: AbortSignal): Promise<SourceDetail> {
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources/${sourceId}/detail`, {
    method: 'GET',
    signal,
  })
}

/** Tombstone a document and enqueue cleanup of its derived artifacts. */
export function deleteDocument(
  sourceId: string,
  documentId: string,
): Promise<{ document_id: string; status: 'deleted' | 'already_deleted'; task_id: string | null }> {
  return apiFetch(
    `/api/v1/spaces/${SPACE_ID}/sources/${sourceId}/documents/${documentId}`,
    { method: 'DELETE' },
  )
}

/** Create a browser-managed upload source. */
export function createUploadSource(uri: string): Promise<CreateSourceResult> {
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources`, {
    method: 'POST',
    body: { source_type: 'upload', uri },
  })
}

export type DeleteSourceResult = {
  source_id: string
  status: 'deleted'
}

/** Delete an empty source. The API refuses sources that hold documents. */
export function deleteSource(sourceId: string): Promise<DeleteSourceResult> {
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources/${sourceId}`, { method: 'DELETE' })
}

/** Create a named folder (a folder-typed source). */
export function createFolder(name: string): Promise<CreateSourceResult> {
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources`, {
    method: 'POST',
    body: { source_type: 'folder', name },
  })
}

export type RenameSourceResult = {
  source_id: string
  name: string
  status: 'renamed'
}

/** Rename a folder (a user-facing source label). */
export function renameSource(sourceId: string, name: string): Promise<RenameSourceResult> {
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources/${sourceId}`, {
    method: 'PATCH',
    body: { name },
  })
}

export type DeleteFolderResult = {
  source_id: string
  status: 'deleted'
  documents_cleared: number
}

/** Delete a folder together with all its documents. */
export function deleteFolder(sourceId: string): Promise<DeleteFolderResult> {
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources/${sourceId}/contents`, {
    method: 'DELETE',
  })
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

/** Trigger ingestion for every document in a source. */
export function triggerIngestion(sourceId: string): Promise<IngestBatchResult> {
  return apiFetch(`/api/v1/spaces/${SPACE_ID}/sources/${sourceId}/ingest`, {
    method: 'POST',
    body: {},
  })
}

/** Get task status. */
export function fetchTaskStatus(taskId: string, signal?: AbortSignal): Promise<TaskInfo> {
  return apiFetch(`/api/v1/tasks/${taskId}`, { method: 'GET', signal })
}

/** Cancel a running task. */
export async function cancelTask(taskId: string): Promise<TaskInfo> {
  try {
    return await apiFetch(`/api/v1/tasks/${taskId}/cancel`, { method: 'POST', body: {} })
  } catch (error) {
    // Older API instances returned 409 when a task finished between polling
    // and cancellation. Reading the authoritative state is equivalent to a
    // successful idempotent cancel from the user's perspective.
    if (error instanceof SourcesApiError && error.status === 409) {
      return fetchTaskStatus(taskId)
    }
    throw error
  }
}

/** Retry a failed task. */
export function retryTask(taskId: string): Promise<IngestResult> {
  return apiFetch(`/api/v1/tasks/${taskId}/retry`, { method: 'POST', body: {} })
}
