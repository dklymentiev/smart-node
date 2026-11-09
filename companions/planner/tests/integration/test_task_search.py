"""Task search matches words, and can be limited to an author.

An agent looking for a task it half remembers types the words it remembers, not
the sentence the title happens to be. The search used to wrap the WHOLE query in
one substring pattern, so the same words in another order found nothing, and an
`author` argument was silently ignored: a query for a name that does not exist
returned the same rows as a query for a real one.

The properties, each on a real database:

  1. every word must occur, in any order, in the title, the description or one
     comment of the same task (the words may sit in different fields)
  2. a word that occurs nowhere makes the task a miss
  3. the whole phrase still finds its task (nothing that worked stops working)
  4. `%`, `_` and a backslash in the query are text, not wildcards
  5. `author` keeps a task when that name wrote one of its comments or filed it
  6. an author who wrote nothing returns nothing
  7. a blank query is refused, as before
  9. a longer query, as an agent types it, needs most of its words, not all, and
     the task holding more of them comes first
  8. another grammatical form finds the task: a query in one form of a word finds
     the other forms of it, in Russian and in English
"""

from __future__ import annotations

import pytest

from planner.repos.actors import ActorsRepo
from planner.repos.ancillary import CommentsRepo
from planner.repos.projects import ProjectsRepo
from planner.repos.tasks import TaskError, TasksRepo


pytestmark = pytest.mark.asyncio


async def _seed(pool):
    actors = ActorsRepo(pool)
    for name in ("admin", "alice", "bob"):
        await actors.create(actor_id=name, display_name=name.title(),
                            type="human" if name == "admin" else "agent")
    project = await ProjectsRepo(pool).create(slug="node", title="Node")
    tasks = TasksRepo(pool)
    comments = CommentsRepo(pool)
    table = await tasks.create(
        title="Release table from the changelog", project_id=project.id,
        requester="alice", description="Collect every version into one table.")
    notes = await tasks.create(
        title="Publish release notes 1.12.52", project_id=project.id, requester="bob",
        description="Marketing sign-off needed first.")
    plain = await tasks.create(
        title="Unrelated chore", project_id=project.id, requester="admin")
    await comments.add(plain.id, author="alice", content="the changelog table is on hold")
    await comments.add(notes.id, author="bob", content="signed by carol, published")
    return tasks, table.id, notes.id, plain.id


async def _ids_ranked(tasks, q, **kw):
    return [t.id for t in await tasks.search(q=q, **kw)]


async def _ids(tasks, q, **kw):
    return sorted(t.id for t in await tasks.search(q=q, **kw))


async def test_words_in_any_order_find_the_task(pool) -> None:
    tasks, table, notes, _ = await _seed(pool)
    assert table in await _ids(tasks, "changelog table release")
    assert notes in await _ids(tasks, "1.12.52 notes release")


async def test_words_may_sit_in_different_fields_of_one_task(pool) -> None:
    tasks, _, notes, _ = await _seed(pool)
    # "Publish" is in the title, "carol" only in a comment
    assert await _ids(tasks, "carol publish") == [notes]


async def test_a_word_that_occurs_nowhere_is_a_miss(pool) -> None:
    tasks, *_ = await _seed(pool)
    assert await _ids(tasks, "release zzzmissingzzz") == []


async def test_the_whole_phrase_still_finds_its_task(pool) -> None:
    tasks, table, *_ = await _seed(pool)
    # five words need three of them: the changelog chore that quotes some also
    # matches, but the task that holds all five comes first
    assert (await _ids_ranked(tasks, "Release table from the changelog"))[0] == table


async def test_wildcard_characters_are_text(pool) -> None:
    tasks, *_ = await _seed(pool)
    assert await _ids(tasks, "100%") == []
    assert await _ids(tasks, "_") == []
    assert await _ids(tasks, "a\\b") == []


async def test_author_keeps_tasks_the_name_wrote_on_or_filed(pool) -> None:
    tasks, table, notes, plain = await _seed(pool)
    # alice filed the table task and commented on the chore
    assert await _ids(tasks, "table", author="alice") == sorted([table, plain])
    # bob filed the notes task and commented on it; "table" is not in his tasks
    assert await _ids(tasks, "table", author="bob") == []
    assert await _ids(tasks, "release", author="bob") == [notes]


async def test_an_author_who_wrote_nothing_returns_nothing(pool) -> None:
    tasks, *_ = await _seed(pool)
    assert await _ids(tasks, "release", author="nobody-xyz") == []


async def test_blank_query_is_refused(pool) -> None:
    tasks, *_ = await _seed(pool)
    with pytest.raises(TaskError):
        await tasks.search(q="   ")


async def test_another_grammatical_form_finds_the_task(pool) -> None:
    await _seed(pool)
    tasks = TasksRepo(pool)
    project = (await ProjectsRepo(pool).list())[0].id
    en = await tasks.create(title="Publish the release notes", project_id=project, requester="admin")
    # another grammatical form finds the task
    assert en.id in await _ids(tasks, "publishing notes")
    # a stem is not a licence to match anything: an unrelated word still misses
    assert await _ids(tasks, "contractors") == []


async def test_a_longer_query_needs_most_of_its_words_and_ranks_by_matches(pool) -> None:
    await _seed(pool)
    tasks = TasksRepo(pool)
    project = (await ProjectsRepo(pool).list())[0].id
    more = await tasks.create(title="alpha beta gamma delta", project_id=project, requester="admin")
    fewer = await tasks.create(title="alpha beta gamma", project_id=project, requester="admin")
    # five words, three or four of them present: both match (three in five is enough)
    found = [t.id for t in await tasks.search(q="alpha beta gamma delta epsilon")]
    assert found == [more.id, fewer.id], "the task with more of the words must come first, not the newer one"
    # two words out of five are not enough
    assert await _ids(tasks, "alpha beta zeta eta theta") == []
    # one or two words still need all of them
    assert await _ids(tasks, "alpha delta") == [more.id]
