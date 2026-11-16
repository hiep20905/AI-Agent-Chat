import React from 'react';
import type { LinkingView } from '../../shared/api/schemaLink';

const shown = (value: unknown) => (Array.isArray(value) ? value.join(', ') : String(value));

/**
 * How a question became a goal: the tables, columns, indicators and values its words named, ranked without a model
 * (a table at score 0 is reached by keys from where the measure lives), then what the model chose among them.
 */
export function LinkingCandidates({ linking }: { linking: LinkingView }) {
  const { candidates, draft } = linking;
  return (
    <section className="ai-linking" aria-label="Ứng viên từ câu hỏi">
      <header className="ai-linking-head">
        <h2>Từ câu hỏi tới mục tiêu</h2>
        <p>
          Tìm ứng viên không dùng model ({linking.ranked_ms} ms), rồi model chọn ({linking.drafted_ms} ms):{' '}
          <em>{draft.reason || (draft.found ? 'đã chọn' : 'không tìm thấy dữ liệu phù hợp')}</em>
        </p>
      </header>
      <div className="ai-linking-grid">
        <div>
          <h3>Bảng ứng viên</h3>
          <ol className="ai-linking-list">
            {candidates.tables.map(table => (
              <li key={table.dataset_id} className={table.score > 0 ? 'is-matched' : 'is-context'}>
                <div className="ai-linking-row">
                  <strong>{table.label ?? table.table}</strong>
                  <code>{table.table}</code>
                  <span className="ai-linking-score">{table.score.toFixed(1)}</span>
                </div>
                <p>{table.reasons.join(' · ')}</p>
                {table.columns.some(column => column.score !== undefined) ? (
                  <ul>
                    {table.columns.filter(column => column.score !== undefined).map(column => (
                      <li key={column.name}>
                        <code>{column.name}</code> {column.role ? <em>{column.role}</em> : null}{' '}
                        {(column.matched ?? []).join(', ')}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ol>
        </div>
        <div>
          <h3>Chỉ số ứng viên</h3>
          {candidates.indicators.length === 0 ? <p className="ai-linking-empty">Không có</p> : (
            <ol className="ai-linking-list">
              {candidates.indicators.map(indicator => (
                <li key={indicator.code} className="is-matched">
                  <div className="ai-linking-row">
                    <strong>{indicator.name ?? indicator.code}</strong>
                    <code>{indicator.code}</code>
                    <span className="ai-linking-score">{indicator.score.toFixed(1)}</span>
                  </div>
                  <p>{indicator.matched.join(', ')}{indicator.unit ? ` · ${indicator.unit}` : ''}</p>
                </li>
              ))}
            </ol>
          )}
          <h3>Giá trị câu hỏi nhắc tới</h3>
          {candidates.values.length === 0 ? <p className="ai-linking-empty">Không có</p> : (
            <ul className="ai-linking-values">
              {candidates.values.map(item => (
                <li key={`${item.dataset_id}.${item.column}.${shown(item.value)}`}>
                  <code>{item.column}</code> = <strong>{shown(item.value)}</strong>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}
