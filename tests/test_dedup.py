from app.dedup import Lead, find_dedupe_candidates, score_pair, build_blocks, group_dedupe_candidates


def test_exact_email_match_scores_high():
    a = Lead(1, "Yuki Aina", "y.aina@singh.io", "singh.io", "8613824247912", "singh logistics")
    b = Lead(2, "Yuki Aina", "y.aina@singh.io", "singh.io", "8613824247912", "singh logistics")
    score, reason = score_pair(a, b)
    assert score >= 0.9
    assert "exact email" in reason


def test_typo_name_with_matching_email_still_flagged():
    a = Lead(1, "Yuki Aina", "y.aina@singh.io", "singh.io", "8613824247912", "singh logistics")
    b = Lead(2, "Yuki Ayna", "y.aina@singh.io", "singh.io", "8613824247912", "singh logistics")
    score, _ = score_pair(a, b)
    assert score >= 0.8


def test_different_email_localpart_same_domain_and_phone_flagged():
    a = Lead(1, "Wei Ming Malik", "weimingm@delta.co", "delta.co", "33615687825", "delta")
    b = Lead(2, "Wei Ming Malik", "wei.ming.m@delta.co", "delta.co", "33615687825", "delta")
    score, reason = score_pair(a, b)
    assert score >= 0.5
    assert "phone" in reason or "email" in reason


def test_same_company_similar_name_different_person_scores_low():
    # two different people, same company, vaguely similar surname -- should
    # NOT be a confident duplicate without an email/phone signal
    a = Lead(1, "John Smith", "j.smith@acme.com", "acme.com", "15550001111", "acme")
    b = Lead(2, "Jane Smith", "jane.smith@acme.com", "acme.com", "15559998888", "acme")
    score, _ = score_pair(a, b)
    assert score <= 0.5


def test_completely_unrelated_leads_score_near_zero():
    a = Lead(1, "Alice Wong", "alice@foo.com", "foo.com", "111", "foo inc")
    b = Lead(2, "Bob Chen", "bob@bar.com", "bar.com", "222", "bar inc")
    score, _ = score_pair(a, b)
    assert score < 0.3


def test_blocking_groups_by_shared_email_domain_and_phone():
    a = Lead(1, "A A", "a@x.com", "x.com", "123456789", "x co")
    b = Lead(2, "B B", "b@y.com", "y.com", "123456789", "y co")  # shares phone
    c = Lead(3, "C C", "c@z.com", "z.com", "987654321", "z co")  # unrelated
    blocks = build_blocks([a, b, c])
    phone_block = blocks.get("phone:123456789")
    assert phone_block is not None
    assert {l.id for l in phone_block} == {1, 2}


def test_find_dedupe_candidates_end_to_end():
    a = Lead(1, "Yuki Aina", "y.aina@singh.io", "singh.io", "8613824247912", "singh logistics")
    b = Lead(2, "Yuki Aina", "y.aina@singh.io", "singh.io", "8613824247912", "singh logistics")
    c = Lead(3, "Totally Different", "different@other.com", "other.com", "000", "other co")
    candidates = find_dedupe_candidates([a, b, c], min_confidence=0.5)
    assert len(candidates) == 1
    assert {candidates[0].lead_id_a, candidates[0].lead_id_b} == {1, 2}


def test_oversized_blocks_are_skipped_for_scalability():
    # a bucket of 60 leads sharing a normalized company should be skipped
    # (not compared pairwise) since it signals a non-selective blocking key
    leads = [
        Lead(i, f"Person {i}", f"p{i}@shared.com", "shared.com", "", "generic co")
        for i in range(60)
    ]
    blocks = build_blocks(leads)
    # the company block exists but should not explode into ~1770 comparisons
    candidates = find_dedupe_candidates(leads, min_confidence=0.5)
    assert len(candidates) == 0


def test_grouping_chains_transitive_pairs_into_one_group():
    # A-B strong match, B-C strong match, A-C never directly compared (no
    # shared blocking key) -- grouping should still cluster all three
    a = Lead(1, "Same Person", "same@x.com", "x.com", "111111111", "acme")
    b = Lead(2, "Same Person", "same@x.com", "x.com", "111111111", "acme")
    c = Lead(3, "Same Person", "same@x.com", "x.com", "111111111", "acme")
    groups = group_dedupe_candidates([a, b, c], min_confidence=0.5)
    assert len(groups) == 1
    assert set(groups[0].member_ids) == {1, 2, 3}


def test_grouping_keeps_unrelated_same_name_people_in_separate_groups():
    # two different real people who happen to share a display_name should
    # NOT be merged into one group just because the name string matches
    a1 = Lead(1, "Common Name", "a1@foo.com", "foo.com", "111", "foo co")
    a2 = Lead(2, "Common Name", "a1@foo.com", "foo.com", "111", "foo co")
    b1 = Lead(3, "Common Name", "b1@bar.com", "bar.com", "222", "bar co")
    b2 = Lead(4, "Common Name", "b1@bar.com", "bar.com", "222", "bar co")
    groups = group_dedupe_candidates([a1, a2, b1, b2], min_confidence=0.5)
    assert len(groups) == 2
    member_sets = [set(g.member_ids) for g in groups]
    assert {1, 2} in member_sets
    assert {3, 4} in member_sets


def test_group_confidence_is_the_weakest_edge_not_average():
    a = Lead(1, "X Y", "xy@z.com", "z.com", "999999999", "z co")
    b = Lead(2, "X Y", "xy@z.com", "z.com", "999999999", "z co")  # exact match w/ a -> 1.0
    c = Lead(3, "X. Y", "xy@z.com", "z.com", "999999999", "z co")  # slightly weaker name match
    groups = group_dedupe_candidates([a, b, c], min_confidence=0.3)
    assert len(groups) == 1
    edge_confidences = [e.confidence for e in groups[0].edges]
    assert groups[0].group_confidence == min(edge_confidences)
