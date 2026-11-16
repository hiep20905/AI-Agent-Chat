import { getJson, sendJson } from './http';

const SCHEMA_LINK_API = '/extensions/text-to-bi/superset-ai-agent/schema-link';

export type GraphLayer = { id: number; name: string; level: number };

export type GraphTable = { dataset_id: number; table: string; label: string | null; layer: GraphLayer | null };

export type GraphEdge = {
  id: number;
  from: number;
  from_column: string;
  to: number;
  to_column: string;
  cardinality: 'many_to_one' | 'one_to_one';
};

export type GraphView = {
  tables: GraphTable[];
  edges: GraphEdge[];
  excluded: Array<{ dataset_id: number; table: string; reason: string }>;
};

export type StageStatus = 'pending' | 'done' | 'failed';
type StageBase = { status: StageStatus; ms: number | null; error?: string };

type Located = { dataset_id: number; table: string; column: string | null; label: string };

export type GoalStage = StageBase & {
  measures?: Array<Located & {
    aggregation: string; source: string; indicator: { code: string; name: string | null; unit: string | null } | null;
  }>;
  group_by?: Array<Located & { time_grain: string | null }>;
  filters?: Array<Located & { operator: string; value: unknown; source: string }>;
  row_kinds?: Array<Located & { value: string; grain: string }>;
  limit?: number;
  order?: 'desc' | 'asc' | null;
};

export type NodesStage = StageBase & { root?: number; terminals?: number[]; reasons?: Record<string, string[]> };

export type GraphStage = StageBase & Partial<GraphView>;

export type PathStep = {
  edge: number; from: number; to: number; forward: boolean; fanout: boolean; cost: number; on: string;
};

export type PathStage = StageBase & {
  algorithm?: 'dijkstra' | 'steiner' | 'steiner_heuristic';
  root?: number;
  cost?: number;
  fanout?: boolean;
  semi_joined?: number[];
  tables?: number[];
  steps?: PathStep[];
  alternatives?: Record<string, number[][]>;
  unreachable?: number[];
  problems?: Array<{ table: number; kind: string; message: string; options: string[] }>;
};

export type AnalysisTrace = {
  status: string;
  message: string | null;
  sql: string | null;
  stages: { goal: GoalStage; nodes: NodesStage; graph: GraphStage; path: PathStage };
  linking?: LinkingView;
};

/** A candidate column as the ranking found it: its role, the values shown, and what of the question matched it. */
export type CandidateColumn = {
  name: string;
  label?: string;
  role?: string;
  unit?: string;
  values?: unknown[];
  score?: number;
  matched?: string[];
};

export type CandidateTable = {
  dataset_id: number;
  table: string;
  label?: string;
  score: number;
  reasons: string[];
  row_kinds?: Array<{ value: string; label: string | null; grain: string }>;
  columns: CandidateColumn[];
};

/** How a question was taken to a goal: candidates ranked without a model, then the goal the model drafted. */
export type LinkingView = {
  question: string;
  ranked_ms: number;
  drafted_ms: number;
  candidates: {
    tables: CandidateTable[];
    indicators: Array<{ code: string; name: string | null; unit: string | null; score: number; matched: string[] }>;
    values: Array<{ dataset_id: number; column: string; value: unknown }>;
  };
  draft: { found: boolean; reason: string };
  goal?: unknown;
};

export async function canUseSchemaLinkLab(): Promise<boolean> {
  try {
    const body = await getJson(`${SCHEMA_LINK_API}/access`);
    return body.can_use_lab === true;
  } catch {
    return false;
  }
}

/** The tables and approved relationships the lab draws before any analysis. */
export function getSchemaGraph(databaseId: number): Promise<GraphView> {
  return getJson(`${SCHEMA_LINK_API}/graph?database_id=${encodeURIComponent(databaseId)}`);
}

export function analyzeGoal(databaseId: number, goal: unknown): Promise<AnalysisTrace> {
  return sendJson(`${SCHEMA_LINK_API}/analyze`, 'POST', { database_id: databaseId, goal });
}

/** A question taken to SQL: the ranked candidates, the drafted goal, then the same four stages as a goal. */
export function linkQuestion(databaseId: number, question: string): Promise<AnalysisTrace> {
  return sendJson(`${SCHEMA_LINK_API}/link`, 'POST', { database_id: databaseId, question });
}
