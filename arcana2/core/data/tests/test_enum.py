from arcana2.data.dimensions.clinical import Clinical

def test_is_parent():
    assert not Clinical.session.is_parent(Clinical.session)
    assert not Clinical.session.is_parent(Clinical.subject)
    assert not Clinical.session.is_parent(Clinical.member)
    assert not Clinical.session.is_parent(Clinical.group)
    assert not Clinical.session.is_parent(Clinical.timepoint)
    assert not Clinical.session.is_parent(Clinical.batch)
    assert not Clinical.session.is_parent(Clinical.matchedpoint)
    assert not Clinical.session.is_parent(Clinical.dataset)

    assert Clinical.member.is_parent(Clinical.session)
    assert Clinical.member.is_parent(Clinical.subject)
    assert not Clinical.member.is_parent(Clinical.member)
    assert not Clinical.member.is_parent(Clinical.group)
    assert not Clinical.member.is_parent(Clinical.timepoint)
    assert not Clinical.member.is_parent(Clinical.batch)
    assert Clinical.member.is_parent(Clinical.matchedpoint)
    assert not Clinical.member.is_parent(Clinical.dataset)

    assert Clinical.group.is_parent(Clinical.session)
    assert Clinical.group.is_parent(Clinical.subject)
    assert not Clinical.group.is_parent(Clinical.member)
    assert not Clinical.group.is_parent(Clinical.group)
    assert not Clinical.group.is_parent(Clinical.timepoint)
    assert Clinical.group.is_parent(Clinical.batch)
    assert not Clinical.group.is_parent(Clinical.matchedpoint)
    assert not Clinical.group.is_parent(Clinical.dataset)

    assert Clinical.timepoint.is_parent(Clinical.session)
    assert not Clinical.timepoint.is_parent(Clinical.subject)
    assert not Clinical.timepoint.is_parent(Clinical.member)
    assert not Clinical.timepoint.is_parent(Clinical.group)
    assert not Clinical.timepoint.is_parent(Clinical.timepoint)
    assert Clinical.timepoint.is_parent(Clinical.batch)
    assert Clinical.timepoint.is_parent(Clinical.matchedpoint)
    assert not Clinical.timepoint.is_parent(Clinical.dataset)

    assert Clinical.subject.is_parent(Clinical.session)
    assert not Clinical.subject.is_parent(Clinical.subject)
    assert not Clinical.subject.is_parent(Clinical.member)
    assert not Clinical.subject.is_parent(Clinical.group)
    assert not Clinical.subject.is_parent(Clinical.timepoint)
    assert not Clinical.subject.is_parent(Clinical.batch)
    assert not Clinical.subject.is_parent(Clinical.matchedpoint)
    assert not Clinical.subject.is_parent(Clinical.dataset)

    assert Clinical.batch.is_parent(Clinical.session)
    assert not Clinical.batch.is_parent(Clinical.subject)
    assert not Clinical.batch.is_parent(Clinical.member)
    assert not Clinical.batch.is_parent(Clinical.group)
    assert not Clinical.batch.is_parent(Clinical.timepoint)
    assert not Clinical.batch.is_parent(Clinical.batch)
    assert not Clinical.batch.is_parent(Clinical.matchedpoint)
    assert not Clinical.batch.is_parent(Clinical.dataset)

    assert Clinical.matchedpoint.is_parent(Clinical.session)
    assert not Clinical.matchedpoint.is_parent(Clinical.subject)
    assert not Clinical.matchedpoint.is_parent(Clinical.member)
    assert not Clinical.matchedpoint.is_parent(Clinical.group)
    assert not Clinical.matchedpoint.is_parent(Clinical.timepoint)
    assert not Clinical.matchedpoint.is_parent(Clinical.batch)
    assert not Clinical.matchedpoint.is_parent(Clinical.matchedpoint)
    assert not Clinical.matchedpoint.is_parent(Clinical.dataset)

    assert Clinical.dataset.is_parent(Clinical.session)
    assert Clinical.dataset.is_parent(Clinical.subject)
    assert Clinical.dataset.is_parent(Clinical.member)
    assert Clinical.dataset.is_parent(Clinical.group)
    assert Clinical.dataset.is_parent(Clinical.timepoint)
    assert Clinical.dataset.is_parent(Clinical.batch)
    assert Clinical.dataset.is_parent(Clinical.matchedpoint)
    assert not Clinical.dataset.is_parent(Clinical.dataset)




