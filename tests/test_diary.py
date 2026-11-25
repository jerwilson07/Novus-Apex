import unittest
from datetime import date
from diary import CHALLENGES, DiaryEvidence, best_candidates, progress, threshold, verified_run, eligible_record, record_fingerprint, review_plan

def row(c, sid=1, **changes):
    name,group,category,activity,variant,size,times=c
    data=dict(id=sid,status="approved",proof="proof",reviewer=99,
        category=category,activity=activity,variant=variant,
        member_ids=",".join(str(i+1) for i in range(size)),
        pb_date="2026-11-10",ms=threshold(times[2]))
    data.update(changes)
    return data

class DiaryTest(unittest.TestCase):
    def test_all_tiers_and_all_teammates(self):
        rows=[row(c,i+1) for i,c in enumerate(CHALLENGES)]
        best=best_candidates(rows,1,date(2026,11,25))
        self.assertEqual(len(best),10)
        self.assertEqual([progress(best,i)[1:] for i in range(3)],[(4,4),(7,7),(10,10)])
        self.assertIn("ToB Duo",best_candidates(rows,2,date(2026,11,25)))
        self.assertNotIn("Inferno",best_candidates(rows,2,date(2026,11,25)))
    def test_group_requirements_not_raw_count(self):
        best=best_candidates([row(c,i+1) for i,c in enumerate(CHALLENGES[:4])],1,date(2026,11,25))
        self.assertEqual(progress(best,0)[1:],(1,4))
        self.assertEqual(progress(best,1)[1:],(2,7))
    def test_unapproved_bad_dates_and_rosters_excluded(self):
        rows=[row(CHALLENGES[0],1,status="denied"),row(CHALLENGES[0],2,member_ids="1"),
              row(CHALLENGES[0],3,pb_date=None),row(CHALLENGES[0],4,proof=None),
              row(CHALLENGES[0],5,pb_date="2027-01-01"),row(CHALLENGES[0],6,member_ids="1,1")]
        self.assertEqual(best_candidates(rows,1,date(2026,11,25)),{})
    def test_threshold_and_best_time(self):
        c=CHALLENGES[0]
        rows=[row(c,1,ms=threshold("24:30")),row(c,2,ms=threshold("24:00"))]
        best=best_candidates(rows,1,date(2026,11,25))
        self.assertEqual(best[c[0]]["id"],2)
        self.assertEqual([progress(best,i)[1] for i in range(3)],[1,1,0])

    def test_wom_join_date_inclusive_for_all_teammates(self):
        run=row(CHALLENGES[0])
        members={"1":{"joined":"2026-11-10"},"2":{"joined":"2026-11-10"}}
        self.assertTrue(verified_run(run,members))
        members["2"]["joined"]="2026-11-11"
        self.assertFalse(verified_run(run,members))
        self.assertFalse(verified_run(run,{"1":{"joined":"2026-11-10"}}))

    def test_verified_progress_ignores_unverified_candidates(self):
        best=best_candidates([row(CHALLENGES[0])],1,date(2026,11,25))
        self.assertEqual(progress(DiaryEvidence(best,{},snapshot_available=True),0)[1],0)
        self.assertEqual(progress(DiaryEvidence(best,best,snapshot_available=True),0)[1],1)

    def test_staff_attestation_cannot_override_applicant_join_date(self):
        run=row(CHALLENGES[0])
        members={"1":{"joined":"2026-11-11"},"2":{"joined":"2026-11-10"}}
        self.assertFalse(eligible_record(run,1,members,{"team_verified":1}))
        self.assertTrue(eligible_record(run,2,members,{"team_verified":1}))
    def test_toa_invocation_must_be_attested(self):
        run=row(CHALLENGES[7])
        members={"1":{"joined":"2026-11-10"}}
        self.assertFalse(eligible_record(run,1,members))
        self.assertTrue(eligible_record(run,1,members,{"invocation_300":1}))
    def test_changed_evidence_requires_new_attestation(self):
        run=row(CHALLENGES[0],guild=123)
        self.assertNotEqual(record_fingerprint(run),record_fingerprint(dict(run,pb_date="2026-11-12")))
        self.assertNotEqual(record_fingerprint(run),record_fingerprint(dict(run,proof="new-proof")))
        self.assertNotEqual(record_fingerprint(run),record_fingerprint(dict(run,member_ids="1,9")))

    def test_one_click_review_plan_respects_rank_and_join_date(self):
        rows=[row(c,i+1) for i,c in enumerate(CHALLENGES)]
        candidates=best_candidates(rows,1,date(2026,11,25))
        membership={"joined":"2026-11-10","tenure_eligible":"2020-01-01"}
        evidence=DiaryEvidence(candidates,{},membership,True)
        for tier,count in enumerate((4,7,10)):
            self.assertEqual(len(review_plan(evidence,tier)),count)
        evidence.membership={"joined":"2026-11-11","tenure_eligible":"2020-01-01"}
        self.assertIsNone(review_plan(evidence,0))
        evidence.membership={"joined":"2026-11-10","tenure_eligible":"2999-01-01"}
        self.assertIsNone(review_plan(evidence,0))
        self.assertIsNone(review_plan(DiaryEvidence(candidates),0))
