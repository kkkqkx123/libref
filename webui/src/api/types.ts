// Wire shapes come from the checked-in OpenAPI schema; no field lists are
// mirrored by hand. Callers send defaulted request fields explicitly.
import type { components } from './schema';

type Schema<K extends keyof components['schemas']> = components['schemas'][K];

export type Envelope<T> = {
  items: T[];
  degraded: boolean;
  count: number;
};

export type JsonValue = Schema<'JsonValue-Output'>;
export type JsonRow = Record<string, JsonValue>;
export type Row = JsonRow;

export type ApiRankRow = Schema<'ApiRankRow'>;
export type ApiArgRow = Schema<'ApiArgRow'>;
export type ApiInsightRow = ApiRankRow | ApiArgRow;
export type PatternInsightRow =
  | Schema<'PatternRow'>
  | Schema<'EvidenceRow'>
  | Schema<'FeatureNodeRow'>
  | Schema<'ClusterRow'>
  | Schema<'PatternHealthRow'>
  | Schema<'PatternLabelRow'>
  | Schema<'PatternSnippetRow'>;
export type SymbolInsightRow = Schema<'SymbolRow'> | Schema<'SignatureRow'>;
export type CallInsightRow = Schema<'CallRow'>;
export type ImportInsightRow = Schema<'ImportEdgeRow'>;
export type InsightRow =
  | ApiInsightRow
  | PatternInsightRow
  | SymbolInsightRow
  | CallInsightRow
  | ImportInsightRow;

export type FeedbackStatus = Schema<'FeedbackItem'>['status'];
export type KnownErrorCode = Schema<'ErrorResponse'>['error'];
export type ApiErrorCode = KnownErrorCode | `http_${number}`;

// Dynamic table columns and legacy feedback links still need value lookup by
// name; domain navigation uses the typed row variants instead.
export function field(row: object, key: string): JsonValue {
  return (row as JsonRow)[key];
}

export type SearchResponse = Schema<'SearchResponse'>;
export type SearchItem = Schema<'SearchItem'>;
export type AnswerResponse = Schema<'AnswerResponse'>;
export type Citation = Schema<'AnswerCitation'>;
export type ReadResponse = Schema<'ReadResponse'>;
export type ChunkLocator = Schema<'ChunkLocator'>;
export type LibraryRow = Schema<'LibraryRow'>;
export type VersionRow = Schema<'VersionRow'>;
export type PageRow = Schema<'PageRow'>;
export type PageList = Schema<'PageList'>;
export type DependencyRow = Schema<'DependencyRow'>;
export type ManifestDependency = Schema<'ManifestDependency'>;
export type IndexRequest = Schema<'IndexRequestItem'>;
export type IndexRequestCreate = Schema<'IndexRequestCreate'>;
export type IndexRequestParams = Schema<'IndexRequestParams'>;
export type DocProbeRequest = Schema<'DocProbeRequest'>;
export type DocProbeResponse = Schema<'DocProbeResponse'>;
export type RepoProbeRequest = Schema<'RepoProbeRequest'>;
export type RepoProbeResponse = Schema<'RepoProbeResponse'>;
export type JobCreate = Schema<'MineJobCreate'> | Schema<'InduceJobCreate'>;
export type JobSummary = Schema<'JobSummary'>;
export type JobDetail =
  | Schema<'MineJobDetail'>
  | Schema<'InduceJobDetail'>
  | Schema<'IndexJobDetail'>;
export type ReviewItem = Schema<'ReviewItem'>;
export type FeedbackItem = Schema<'FeedbackItem'>;
export type Principal = Schema<'PrincipalResponse'>;
export type PrincipalSummary = Schema<'PrincipalSummary'>;
export type ApiKeyRow = Schema<'ApiKeyRow'>;
export type IssuedKey = Schema<'IssuedKey'>;
export type QuotaPolicy = Schema<'QuotaPolicy'>;
export type DiscoveryCandidate = Schema<'DiscoveryCandidate'>;
export type ResolutionResponse = Schema<'ResolutionResponse'>;
export type MiningHealthResponse = Schema<'MiningHealthResponse'>;
export type HealthResponse = Schema<'HealthResponse'>;
export type ReadyResponse = Schema<'ReadyResponse'>;
export type MetricsResponse = Schema<'MetricsResponse'>;
export type OkResponse = Schema<'OkResponse'>;
export type OkIdResponse = Schema<'OkIdResponse'>;
export type ConfigRefreshResponse = Schema<'ConfigRefreshResponse'>;
