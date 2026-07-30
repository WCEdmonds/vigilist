import type { CaseRole } from '../types';

/* A case role is an assertion about someone's standing in THIS matter, which
   the documents deliberately do not contain: a civil matter built from a
   criminal case file mentions the criminal parties constantly and the civil
   plaintiffs barely, so mention frequency can never identify the caption.

   These strings are persisted in entities.attributes.case_role and must stay
   in sync with models.CASE_ROLES on the backend. */

/** Billing order for declared parties: a matter opens on its caption. */
export const CASE_ROLE_ORDER: CaseRole[] = [
  'plaintiff',
  'defendant',
  'plaintiff_counsel',
  'defense_counsel',
  'witness',
];

export const CASE_ROLE_LABELS: Record<CaseRole, string> = {
  plaintiff: 'Plaintiff',
  defendant: 'Defendant',
  plaintiff_counsel: "Plaintiff's counsel",
  defense_counsel: 'Defense counsel',
  witness: 'Witness',
};

/** Sort key. Unassigned entities rank after every declared role. */
export function caseRoleRank(role: CaseRole | null | undefined): number {
  const i = role ? CASE_ROLE_ORDER.indexOf(role) : -1;
  return i === -1 ? CASE_ROLE_ORDER.length : i;
}
