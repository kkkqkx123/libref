// Wire shapes come from the checked-in OpenAPI schema; no field lists are
// mirrored by hand. Callers send defaulted request fields explicitly.
import type { components } from './schema';

type Schema<K extends keyof components['schemas']> = components['schemas'][K];

export type Envelope<T> = Pick<Schema<'ReviewResponse'>, 'degraded' | 'count'> & {
  items: T[];
};

export type SearchResponse = Schema<'SearchResponse'>;
export type SearchItem = Schema<'SearchItem'>;
export type AnswerResponse = Schema<'AnswerResponse'>;
export type Citation = Schema<'AnswerCitation'>;
export type ReadResponse = Schema<'ReadResponse'>;
export type ChunkLocator = NonNullable<ReadResponse['chunk_details']>[number];
export type LibraryRow = Schema<'LibraryRow'>;
export type VersionRow = Schema<'VersionRow'>;
export type PageRow = Schema<'PageRow'>;
export type PageList = Schema<'PageList'>;
export type IndexRequest = Schema<'IndexRequestItem'>;
export type IndexRequestCreate = Schema<'IndexRequestCreate'>;
export type DocProbeRequest = Schema<'DocProbeRequest'>;
export type RepoProbeRequest = Schema<'RepoProbeRequest'>;
export type JobSummary = Schema<'JobSummary'>;
export type JobDetail = Schema<'JobDetail'>;
export type ReviewItem = Schema<'ReviewItem'>;
export type FeedbackItem = Schema<'FeedbackItem'>;
export type Principal = Schema<'PrincipalResponse'>;
export type PrincipalSummary = Schema<'PrincipalSummary'>;
export type ApiKeyRow = Schema<'ApiKeyRow'>;
export type IssuedKey = Schema<'IssuedKey'>;
export type QuotaPolicy = Schema<'QuotaPolicy'>;
export type DiscoveryCandidate = Schema<'DiscoveryCandidate'>;
export type DocProbeResponse = Schema<'DocProbeResponse'>;
export type RepoProbeResponse = Schema<'RepoProbeResponse'>;
export type ResolutionResponse = Schema<'ResolutionResponse'>;
export type MiningHealthResponse = Schema<'MiningHealthResponse'>;
export type HealthResponse = Schema<'HealthResponse'>;
export type ReadyResponse = Schema<'ReadyResponse'>;
export type OkResponse = Schema<'OkResponse'>;
export type OkIdResponse = Schema<'OkIdResponse'>;
export type ConfigRefreshResponse = Schema<'ConfigRefreshResponse'>;
export type Row = Record<string, unknown>;
