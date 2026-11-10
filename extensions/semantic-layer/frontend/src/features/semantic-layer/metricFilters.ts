import { isNumericColumnType } from './metricFormat';
import type { MetricFilterOperator, MetricQueryColumn, MetricQueryFilter } from './types';

/** A filter row as typed: the value stays text until the payload is built. */
export type FilterDraft = { key: number; column: string; operator: MetricFilterOperator; value: string };

export const OPERATORS: Array<{ value: MetricFilterOperator; label: string }> = [
  { value: '==', label: '=' },
  { value: '!=', label: '≠' },
  { value: '>', label: '>' },
  { value: '>=', label: '≥' },
  { value: '<', label: '<' },
  { value: '<=', label: '≤' },
  { value: 'IN', label: 'thuộc' },
  { value: 'NOT IN', label: 'không thuộc' },
];

export const columnLabel = (column: Pick<MetricQueryColumn, 'column_name' | 'verbose_name'>) => (
  column.verbose_name || column.column_name
);
export const isListOperator = (operator: MetricFilterOperator) => operator === 'IN' || operator === 'NOT IN';

/** Turns the typed filter rows into the execute payload, converting values for numeric columns. */
export function buildFilters(drafts: FilterDraft[], columns: MetricQueryColumn[]): MetricQueryFilter[] {
  return drafts.map(draft => {
    const column = columns.find(item => item.column_name === draft.column);
    if (!column) throw new Error('Chọn cột cho mọi bộ lọc.');
    const numeric = isNumericColumnType(column.type);
    const convert = (text: string) => {
      if (!numeric) return text;
      const number = Number(text);
      if (!Number.isFinite(number)) throw new Error(`Giá trị lọc của cột ${columnLabel(column)} phải là số.`);
      return number;
    };
    if (isListOperator(draft.operator)) {
      const parts = draft.value.split(',').map(part => part.trim()).filter(Boolean);
      if (parts.length === 0) throw new Error(`Nhập ít nhất một giá trị cho cột ${columnLabel(column)}.`);
      return { column: column.column_name, operator: draft.operator, value: parts.map(convert) };
    }
    if (!draft.value.trim()) throw new Error(`Nhập giá trị lọc cho cột ${columnLabel(column)}.`);
    return { column: column.column_name, operator: draft.operator, value: convert(draft.value.trim()) };
  });
}
