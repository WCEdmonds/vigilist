import { useState } from 'react';
import AiRunConfirm, { type AiRunRequest } from '../components/AiRunConfirm';

/**
 * Gate a paid AI run behind the shared cost confirmation:
 *   const { confirmAiRun, aiRunDialog } = useAiRunConfirm(productionId);
 *   <button onClick={() => confirmAiRun({ run: () => start() })}>Run</button>
 *   {aiRunDialog}
 */
export function useAiRunConfirm(productionId: number) {
  const [request, setRequest] = useState<AiRunRequest | null>(null);
  const confirmAiRun = (r: AiRunRequest) => setRequest(r);
  const aiRunDialog = request
    ? <AiRunConfirm productionId={productionId} request={request} onClose={() => setRequest(null)} />
    : null;
  return { confirmAiRun, aiRunDialog };
}
