import { useState } from 'react';
import AIReviewLane from './AIReviewLane';
import HumanReviewLane from './HumanReviewLane';
import type { ProductionInfo } from '../types';

interface Props {
  production: ProductionInfo;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
}

/**
 * Full-screen review workspace: the AI review lane and the human review
 * (queue/batch) lane side by side, so attorneys can triage an AI slice and
 * hand it straight to a queue without leaving the page.
 */
export default function ReviewWorkspace({ production, onViewDocument, onBack }: Props) {
  // Bumped whenever the AI lane creates a queue from a slice, so the human
  // lane's queue list refetches and shows it without a manual reload.
  const [queueRefreshKey, setQueueRefreshKey] = useState(0);

  return (
    <div className="review-workspace">
      <div className="review-workspace-header">
        <button className="btn-header" onClick={onBack}>← Back</button>
        <span className="review-workspace-title">Review</span>
        <span className="review-workspace-production">{production.name}</span>
      </div>
      <div className="review-lanes">
        <div className="review-lane-ai">
          <AIReviewLane
            productionId={production.id}
            docCount={production.document_count}
            caseContext={production.case_context ?? null}
            onViewDocument={onViewDocument}
            onQueueCreated={() => setQueueRefreshKey(k => k + 1)}
          />
        </div>
        <div className="review-lane-human">
          <HumanReviewLane productionId={production.id} refreshKey={queueRefreshKey} />
        </div>
      </div>
    </div>
  );
}
