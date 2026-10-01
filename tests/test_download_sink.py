"""`storage/download_sink.py`: the file a download becomes, and when.

Two properties carry the module, and both are about *when* rather than *what*:
a part file never reaches the final name except through `commit`, and `commit`
is a rename rather than a copy. The naming tests are the other half -- they are
where the platform's own rules (reserved names, separators, trailing dots, the
byte cap) are applied, because the lease carries a verbatim title and only this
side knows what filesystem it is writing to.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from wt_media_agent.storage import DownloadSink, InsufficientSpaceError, NameUnusableError
from wt_media_agent.storage.download_sink import MAX_NAME_BYTES, FREE_SPACE_MARGIN_BYTES

#: The day every naming test files under. The date subdirectory is derived from
#: this argument, so pinning it keeps the assertions about the shape and not
#: about whatever day the test happens to run.
TODAY = date(2026, 9, 30)


class SinkTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.sink = DownloadSink(self.directory)


class FileNameTest(SinkTestCase):
    def test_the_game_name_leads_and_the_material_id_says_which_one(self):
        self.assertEqual(
            self.sink.file_name("春日", 42, "mp4", TODAY), "20260930/春日-42.mp4"
        )

    def test_the_date_subdirectory_is_today(self):
        self.assertEqual(
            self.sink.file_name("三角洲行动", 30, "mp4", date(2026, 9, 30)),
            "20260930/三角洲行动-30.mp4",
        )

    def test_separators_and_wildcards_become_underscores(self):
        """A game name is a material's label, not a path, and can contain anything."""
        name = self.sink.file_name('a/b\\c:d*e?f"g<h>i|j', 1, "mp4", TODAY)
        self.assertEqual(name, "20260930/a_b_c_d_e_f_g_h_i_j-1.mp4")
        # The one separator left is the date's own; none of the title's rode along.
        self.assertEqual(name.count("/"), 1)
        self.assertNotIn("\\", name)

    def test_a_reserved_device_name_is_defused(self):
        """Windows refuses `CON` as a file name, so the name may not be one.

        The Agent runs on both platforms from one codebase, so the stricter set
        is applied everywhere rather than only where it is enforced. Without
        this the download fails on the operator's machine and not on ours.
        """
        for reserved in ("CON", "nul", "COM1", "LPT9"):
            name = self.sink.file_name(reserved, 1, "mp4", TODAY)
            self.assertTrue(name.startswith("20260930/_"), name)

    def test_trailing_dots_and_spaces_are_stripped(self):
        """Windows drops them, so `a.` and `a` would name one file."""
        self.assertEqual(self.sink.file_name("name... ", 1, "mp4", TODAY), "20260930/name-1.mp4")

    def test_a_game_name_that_leaves_nothing_is_refused_rather_than_invented(self):
        """The executor reports `download_name_unusable`; it does not guess.

        Inventing a name would put a file on somebody's disk that they cannot
        connect to the material it came from. What is refused is a game name
        that leaves *nothing* -- empty, or only dots and spaces, which strip
        away to empty or name a directory.
        """
        for game_name in ("", "   ", "...", ".", "..", ". . ."):
            with self.assertRaises(NameUnusableError, msg=game_name):
                self.sink.file_name(game_name, 1, "mp4", TODAY)

    def test_a_game_name_of_only_illegal_characters_still_names_its_material(self):
        """The other side of the same decision, pinned so it is a choice.

        `???` sanitizes to `___`, which is a legal name and carries no
        information -- but the material id in the name does, so the file is
        still connectable to the material it came from. Refusing here would fail
        a download over a cosmetic property of somebody's name.
        """
        self.assertEqual(self.sink.file_name("???", 42, "mp4", TODAY), "20260930/___-42.mp4")

    def test_a_very_long_game_name_stays_inside_the_byte_cap(self):
        """255 bytes, and bytes rather than characters.

        A 300-character Chinese name is 900 bytes; a cap counted in characters
        would write a name the filesystem refuses, which surfaces as an
        `OSError` from the middle of a download rather than as a finding here.
        """
        name = self.sink.file_name("春" * 300, 7, "mp4", TODAY)
        self.assertLessEqual(len(name.encode("utf-8")), MAX_NAME_BYTES)
        self.assertTrue(name.endswith("-7.mp4"))
        # The date prefix has to come out of the same cap, not beside it.
        self.assertTrue(name.startswith("20260930/"))

    def test_the_extension_is_normalized(self):
        self.assertEqual(self.sink.file_name("t", 1, ".MP4", TODAY), "20260930/t-1.mp4")
        self.assertEqual(self.sink.file_name("t", 1, "mp4", TODAY), "20260930/t-1.mp4")
        self.assertEqual(self.sink.file_name("t", 1, "", TODAY), "20260930/t-1.bin")
        self.assertEqual(self.sink.file_name("t", 1, "m/p4", TODAY), "20260930/t-1.mp4")

    def test_a_control_character_cannot_ride_along(self):
        self.assertEqual(self.sink.file_name("a\x00b\x1fc", 1, "mp4", TODAY), "20260930/a_b_c-1.mp4")

    def test_a_publish_time_adds_its_segment_before_the_extension(self):
        name = self.sink.file_name("三角洲行动", 30, "mp4", TODAY, date(2026, 9, 22))
        self.assertEqual(name, "20260930/三角洲行动-30-20260922.mp4")

    def test_an_absent_publish_time_omits_the_segment_entirely(self):
        """The material has no publish time; naming it must not invent one."""
        self.assertEqual(
            self.sink.file_name("三角洲行动", 30, "mp4", TODAY),
            "20260930/三角洲行动-30.mp4",
        )

    def test_a_title_adds_its_segment_after_the_id(self):
        name = self.sink.file_name("三角洲行动", 30, "mp4", TODAY, None, "一把三千万 最肥的一局")
        self.assertEqual(name, "20260930/三角洲行动-30-一把三千万 最肥的一局.mp4")

    def test_publish_time_and_title_join_in_the_contract_order(self):
        """`日期/游戏名-ID-发布时间-标题名`, each segment where the shape says."""
        name = self.sink.file_name(
            "三角洲行动", 30, "mp4", TODAY, date(2026, 9, 22), "一把三千万 最肥的一局"
        )
        self.assertEqual(name, "20260930/三角洲行动-30-20260922-一把三千万 最肥的一局.mp4")

    def test_a_title_keeps_its_hash_and_its_full_width_marks(self):
        """`#`, `@` and full-width punctuation are legal on disk and survive."""
        name = self.sink.file_name("三角洲行动", 30, "mp4", TODAY, None, "一把三千万！ #左梓轩小叮当")
        self.assertEqual(name, "20260930/三角洲行动-30-一把三千万！ #左梓轩小叮当.mp4")

    def test_a_title_too_long_for_the_cap_is_truncated_not_dropped(self):
        """255 bytes counts the whole name; the title is what gives way.

        The title is the segment the truncation is for -- the game and id say
        which material, the title only says more -- so it is cut from the tail,
        on a character boundary, and the extension is never lost.
        """
        name = self.sink.file_name("三角洲行动", 30, "mp4", TODAY, None, "春" * 100)
        self.assertLessEqual(len(name.encode("utf-8")), MAX_NAME_BYTES)
        self.assertTrue(name.endswith(".mp4"))
        self.assertTrue(name.startswith("20260930/三角洲行动-30-"))

    def test_a_title_does_not_let_a_long_game_use_the_whole_budget(self):
        """With a title the game is capped at half, or the title never appears.

        A 900-byte game name would otherwise consume the whole cap and the title
        -- the one segment the truncation is for -- would always vanish behind it.
        """
        name = self.sink.file_name("春" * 300, 7, "mp4", TODAY, None, "标题")
        self.assertLessEqual(len(name.encode("utf-8")), MAX_NAME_BYTES)
        self.assertTrue(name.endswith("标题.mp4"))
        self.assertIn("-7-", name)


class AllocateTest(SinkTestCase):
    def test_a_free_name_is_used_as_it_is(self):
        self.assertEqual(self.sink.allocate("a-1.mp4"), "a-1.mp4")

    def test_a_taken_name_gets_the_platform_convention(self):
        """A second download of one material keeps both files.

        Overwriting would lose the first; failing would lose the second. The
        operator asked for both, so the name is made to say which is which.

        The marker goes before the extension -- `a-1 (2).mp4`, not
        `a-1.mp4 (2)` -- because the extension is how the desktop decides what
        opens the file, and a name ending in ` (2)` has none.
        """
        (self.directory / "a-1.mp4").write_bytes(b"first")
        self.assertEqual(self.sink.allocate("a-1.mp4"), "a-1 (2).mp4")

    def test_it_walks_past_every_taken_suffix(self):
        (self.directory / "a.mp4").write_bytes(b"x")
        (self.directory / "a (2).mp4").write_bytes(b"x")
        self.assertEqual(self.sink.allocate("a.mp4"), "a (3).mp4")

    def test_a_collision_suffix_cannot_push_the_name_over_the_cap(self):
        name = self.sink.file_name("春" * 300, 7, "mp4", TODAY)
        (self.directory / name).parent.mkdir(parents=True)
        (self.directory / name).write_bytes(b"x")
        allocated = self.sink.allocate(name)
        self.assertLessEqual(len(allocated.encode("utf-8")), MAX_NAME_BYTES)
        self.assertTrue(allocated.endswith(" (2).mp4"))

    def test_a_name_with_a_date_subdirectory_collides_within_it(self):
        """The collision marker goes before the extension, inside the subdirectory."""
        (self.directory / "20260930").mkdir()
        (self.directory / "20260930" / "a-1.mp4").write_bytes(b"first")
        self.assertEqual(
            self.sink.allocate("20260930/a-1.mp4"), "20260930/a-1 (2).mp4"
        )

    def test_a_name_with_no_extension_still_collides_correctly(self):
        (self.directory / "plain").write_bytes(b"x")
        self.assertEqual(self.sink.allocate("plain"), "plain (2)")

    def test_a_name_handed_in_at_the_cap_is_still_shortened_for_its_marker(self):
        """`allocate` must hold the cap on its own, not rely on its caller.

        `file_name` reserves room for a marker, so a name it produced always has
        some -- which is exactly why this uses a name it did not produce. The
        method is public and its caller may be a resumed download reading a name
        out of a checkpoint, so "the names I return are usable" has to be true of
        `allocate` by itself.
        """
        at_the_cap = "春" * 83 + ".mp4"
        self.assertEqual(len(at_the_cap.encode("utf-8")), MAX_NAME_BYTES - 2)
        (self.directory / at_the_cap).write_bytes(b"x")

        allocated = self.sink.allocate(at_the_cap)
        self.assertLessEqual(len(allocated.encode("utf-8")), MAX_NAME_BYTES)
        self.assertTrue(allocated.endswith(" (2).mp4"))


class PartFileTest(SinkTestCase):
    def test_the_part_lives_inside_the_save_directory(self):
        """Which is what makes the final rename atomic.

        `os.replace` is only atomic within one filesystem. A part in the system
        temp directory is routinely on a different one, and there the rename
        degrades to a copy -- which can be interrupted half-way, leaving a file
        that is present and wrong. The part is placed here so that cannot happen.
        """
        part = self.sink.part_path("task-1")
        self.assertTrue(str(part).startswith(str(self.directory)))

    def test_a_task_id_cannot_escape_the_part_directory(self):
        """Task ids come from Cloud; a path in one is a traversal."""
        part = self.sink.part_path("../../etc/passwd")
        self.assertEqual(part.parent, self.sink.part_directory())
        self.assertNotIn("..", part.name)

    def test_the_offset_is_the_file_not_the_bookkeeping(self):
        """A checkpoint lags behind the bytes; the file does not."""
        self.assertEqual(self.sink.resume_offset("task-1"), 0)
        self.sink.append("task-1", b"abc")
        self.assertEqual(self.sink.resume_offset("task-1"), 3)

    def test_appends_accumulate(self):
        self.sink.append("task-1", b"abc")
        self.sink.append("task-1", b"def")
        self.assertEqual(self.sink.part_path("task-1").read_bytes(), b"abcdef")

    def test_discarding_leaves_nothing_behind(self):
        """What a caller does after a server ignored its `Range`."""
        self.sink.append("task-1", b"abc")
        self.sink.discard("task-1")
        self.assertEqual(self.sink.resume_offset("task-1"), 0)
        self.assertFalse(self.sink.part_directory().exists())

    def test_discarding_what_was_never_there_is_not_an_error(self):
        self.sink.discard("never-started")

    def test_a_part_that_never_commits_never_becomes_the_file(self):
        self.sink.append("task-1", b"half")
        self.assertFalse((self.directory / "a-1.mp4").exists())
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), [".wt-media-part"])


class CommitTest(SinkTestCase):
    def test_committing_makes_it_the_file_and_empties_the_part_directory(self):
        self.sink.append("task-1", b"abcdef")
        final = self.sink.commit("task-1", "a-1.mp4")

        self.assertEqual(final.read_bytes(), b"abcdef")
        self.assertFalse(self.sink.part_directory().exists())

    def test_committing_leaves_no_part_to_resume_from(self):
        self.sink.append("task-1", b"abcdef")
        self.sink.commit("task-1", "a-1.mp4")
        self.assertEqual(self.sink.resume_offset("task-1"), 0)

    def test_a_commit_over_an_existing_file_replaces_it(self):
        """`allocate` is what avoids this; `commit` is only ever a rename."""
        (self.directory / "a-1.mp4").write_bytes(b"old")
        self.sink.append("task-1", b"new")
        self.sink.commit("task-1", "a-1.mp4")
        self.assertEqual((self.directory / "a-1.mp4").read_bytes(), b"new")

    def test_committing_into_a_date_subdirectory_creates_it(self):
        """The date directory does not exist until a download lands in it."""
        self.sink.append("task-1", b"abcdef")
        final = self.sink.commit("task-1", "20260930/a-1.mp4")

        self.assertEqual(final.read_bytes(), b"abcdef")
        self.assertEqual(final.parent, self.directory / "20260930")


class SpaceTest(SinkTestCase):
    def test_room_is_measured_with_a_margin(self):
        """Exactly-enough room is not enough.

        The filesystem needs room for its own bookkeeping, and the part grows
        while this number ages.
        """
        with mock.patch.object(self.sink, "free_bytes", return_value=FREE_SPACE_MARGIN_BYTES):
            with self.assertRaises(InsufficientSpaceError):
                self.sink.require_room(1)
        with mock.patch.object(self.sink, "free_bytes", return_value=FREE_SPACE_MARGIN_BYTES + 1):
            self.sink.require_room(1)

    def test_an_unknown_length_is_not_a_reason_to_refuse(self):
        """`-1` means the source never declared a size.

        Refusing here would turn "the server sent no Content-Range" into a
        download that cannot start.
        """
        with mock.patch.object(self.sink, "free_bytes", return_value=0):
            self.sink.require_room(-1)

    def test_an_unreadable_volume_is_not_a_refusal(self):
        with mock.patch.object(self.sink, "free_bytes", return_value=-1):
            self.sink.require_room(10 ** 12)

    def test_free_bytes_is_a_real_reading_of_a_real_directory(self):
        self.assertGreater(self.sink.free_bytes(), 0)

    def test_writability_is_asked_of_the_directory(self):
        self.assertTrue(self.sink.is_writable())
        self.assertFalse(DownloadSink(self.directory / "nope").is_writable())

    def test_a_writable_file_is_not_a_writable_save_directory(self):
        """`os.access` cannot tell the two apart, and the answer differs.

        A missing path already answers `False` to `os.access`, so a check that
        only asked `os.access` would look correct right up until the chosen save
        directory became a file -- which is a state an operator can reach by
        moving something onto it.
        """
        path = self.directory / "not-a-directory"
        path.write_bytes(b"x")
        os.chmod(path, 0o600)
        self.assertTrue(os.access(path, os.W_OK))
        self.assertFalse(DownloadSink(path).is_writable())

    def test_reading_a_part_back_gives_the_bytes_that_were_appended(self):
        """A resumed attempt hashes what is already there before the new bytes.

        Without this read the digest covers the tail alone, and the check it
        feeds would pass on a file assembled from two different objects.
        """
        self.sink.append("task-1", b"abc")
        self.sink.append("task-1", b"def")

        self.assertEqual(b"".join(self.sink.part_chunks("task-1")), b"abcdef")

    def test_reading_a_part_that_is_not_there_iterates_nothing(self):
        """A missing part is `resume_offset`'s zero, seen from the other side."""
        self.assertEqual(list(self.sink.part_chunks("task-1")), [])

    def test_the_read_chunk_size_does_not_change_what_comes_back(self):
        self.sink.append("task-1", b"abcdef")

        self.assertEqual(list(self.sink.part_chunks("task-1", chunk_bytes=2)), [b"ab", b"cd", b"ef"])


class ShardedPartTest(SinkTestCase):
    """The part files a parallel download writes, and the merge that ends it.

    A sharded download cannot use one append-only file per task: N threads
    appending to one file interleave their bytes, giving a file of the right
    length holding the wrong object. So each shard owns a file and the merge puts
    them back in order. What each file is worth is unchanged -- its size is how
    much of that shard arrived -- which is what keeps resume working with no new
    place to record progress.
    """

    def test_a_shard_part_is_a_file_of_its_own_beside_the_single_stream_part(self):
        single = self.sink.part_path("task-1")
        shards = [self.sink.part_path("task-1", index) for index in range(3)]

        self.assertEqual(len(set(shards)), 3)
        self.assertNotIn(single, shards)
        self.assertTrue(all(path.parent == self.sink.part_directory() for path in shards))

    def test_a_negative_shard_index_is_refused(self):
        """Shard numbers are positions in a plan; there is no shard before the first."""
        with self.assertRaises(ValueError):
            self.sink.part_path("task-1", -1)

    def test_each_shard_measures_only_its_own_bytes(self):
        """The whole resume story is this: a shard's size is that shard's progress."""
        self.sink.append_shard("task-1", 0, b"abc")
        self.sink.append_shard("task-1", 1, b"de")

        self.assertEqual(self.sink.shard_offset("task-1", 0), 3)
        self.assertEqual(self.sink.shard_offset("task-1", 1), 2)

    def test_a_shard_that_never_arrived_measures_zero(self):
        """A shard with no file is the same "nothing yet" as a missing part file."""
        self.assertEqual(self.sink.shard_offset("task-1", 0), 0)

    def test_assembling_shards_writes_them_in_number_order(self):
        """Order is the whole content of the merge -- shard 1 is not shard 0."""
        for index, chunk in enumerate([b"ab", b"cd", b"ef"]):
            self.sink.append_shard("task-1", index, chunk)

        seen = []
        written = self.sink.assemble_shards("task-1", 3, seen.append)

        self.assertEqual(self.sink.part_path("task-1").read_bytes(), b"abcdef")
        self.assertEqual(written, 6)
        self.assertEqual(b"".join(seen), b"abcdef")

    def test_assembling_truncates_a_merge_an_interrupted_attempt_left_behind(self):
        """A half-merged part from a crash is overwritten, not appended to.

        The merge can only be interrupted by the process dying, so the next
        attempt finds a part file longer than it is about to write. Opening it
        for append would leave the tail of the previous object in the result.
        """
        self.sink.append("task-1", b"stale-stale-stale")
        self.sink.append_shard("task-1", 0, b"ab")
        self.sink.append_shard("task-1", 1, b"cd")

        self.sink.assemble_shards("task-1", 2, lambda chunk: None)

        self.assertEqual(self.sink.part_path("task-1").read_bytes(), b"abcd")

    def test_shard_indices_names_the_shards_that_are_there(self):
        """Which shard files exist is what tells a stale plan from the current one."""
        self.sink.append_shard("task-1", 2, b"x")
        self.sink.append_shard("task-1", 0, b"x")

        self.assertEqual(self.sink.shard_indices("task-1"), [0, 2])

    def test_shard_indices_does_not_count_the_single_stream_part(self):
        """The one file that is not a shard must not be read as shard zero."""
        self.sink.append("task-1", b"xxx")

        self.assertEqual(self.sink.shard_indices("task-1"), [])

    def test_shard_indices_of_a_task_with_nothing_on_disk_is_empty(self):
        self.assertEqual(self.sink.shard_indices("task-1"), [])

    def test_written_bytes_is_what_the_shards_hold_together(self):
        """What a failure report means by "how far it got" with N shards in flight."""
        self.sink.append_shard("task-1", 0, b"de")
        self.sink.append_shard("task-1", 1, b"fghi")

        self.assertEqual(self.sink.written_bytes("task-1"), 6)

    def test_written_bytes_does_not_double_count_a_merge_that_just_finished(self):
        """The merged part and the shards are two views of one number, not two halves.

        Between the merge and the commit both layouts are on disk holding the
        whole object. Adding them would report twice the file -- and it is not a
        theoretical window: a digest that fails right after a merge is exactly a
        failure reported from inside it.
        """
        for index, chunk in enumerate([b"ab", b"cd"]):
            self.sink.append_shard("task-1", index, chunk)
        self.sink.assemble_shards("task-1", 2, lambda chunk: None)

        self.assertEqual(self.sink.part_path("task-1").read_bytes(), b"abcd")
        self.assertEqual(self.sink.written_bytes("task-1"), 4)

    def test_written_bytes_follows_the_shards_while_a_merge_is_still_running(self):
        """A half-written merge is behind the shards, and the shards are the truth."""
        for index, chunk in enumerate([b"ab", b"cd"]):
            self.sink.append_shard("task-1", index, chunk)
        self.sink.append("task-1", b"a")

        self.assertEqual(self.sink.written_bytes("task-1"), 4)

    def test_written_bytes_leaves_a_task_whose_id_starts_the_same_alone(self):
        """`task-1` and `task-10` share a prefix; only one of them owns these files.

        A prefix match without the `.part` boundary would count, and discard, a
        neighbouring task's bytes -- a wrong number that reports as progress.
        """
        self.sink.append_shard("task-10", 0, b"x" * 5)

        self.assertEqual(self.sink.written_bytes("task-1"), 0)
        self.assertEqual(self.sink.written_bytes("task-10"), 5)

    def test_discarding_removes_every_shard_as_well_as_the_single_stream_part(self):
        self.sink.append("task-1", b"abc")
        self.sink.append_shard("task-1", 0, b"d")
        self.sink.append_shard("task-1", 1, b"e")

        self.sink.discard("task-1")

        self.assertEqual(self.sink.written_bytes("task-1"), 0)
        self.assertFalse(self.sink.part_directory().exists())

    def test_discarding_leaves_a_task_whose_id_starts_the_same_alone(self):
        self.sink.append_shard("task-10", 0, b"x" * 5)

        self.sink.discard("task-1")

        self.assertEqual(self.sink.written_bytes("task-10"), 5)

    def test_committing_removes_the_shard_parts_and_the_directory(self):
        """A shard file left behind keeps the part directory -- and its bytes -- alive."""
        for index, chunk in enumerate([b"ab", b"cd"]):
            self.sink.append_shard("task-1", index, chunk)
        self.sink.assemble_shards("task-1", 2, lambda chunk: None)

        final = self.sink.commit("task-1", "a-1.mp4")

        self.assertEqual(final.read_bytes(), b"abcd")
        self.assertFalse(self.sink.part_directory().exists())


if __name__ == "__main__":
    unittest.main()
