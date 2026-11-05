#!/usr/bin/env python3
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tag_ski_routes import enough_tagged, escape, is_nordic, main, members_of, tag_way_line


class IsNordic(unittest.TestCase):
    def test_is_nordic(self):
        self.assertTrue(is_nordic("nordic"))
        self.assertTrue(is_nordic("downhill;nordic"))
        self.assertTrue(is_nordic("nordic; hike"))
        self.assertFalse(is_nordic("nordics"))
        self.assertFalse(is_nordic("downhill"))


class MembersOf(unittest.TestCase):
    def test_members_are_ways_only_and_carry_grooming(self):
        lines = ["r5 Tpiste:type=nordic,piste:grooming=classic Mw1@,n3@,r9@,w2@"]
        self.assertEqual(members_of(lines), {"1": "classic", "2": "classic"})

    def test_a_relation_not_nordic_is_skipped(self):
        self.assertEqual(members_of(["r5 Tpiste:type=downhill Mw1@"]), {})

    def test_prepared_beats_backcountry_across_relations(self):
        a = "r5 Tpiste:type=nordic,piste:grooming=backcountry Mw1@"
        b = "r6 Tpiste:type=nordic,piste:grooming=classic Mw1@"
        self.assertEqual(members_of([a, b]), {"1": "classic"})
        self.assertEqual(members_of([b, a]), {"1": "classic"})

    def test_an_area_is_not_a_route(self):
        self.assertEqual(members_of(["r5 Ttype=multipolygon,piste:type=nordic Mw1@"]), {})

    def test_a_space_in_the_value_is_read(self):
        self.assertEqual(members_of(["r5 Tpiste:type=hike;%20%nordic Mw1@"]), {"1": None})

    def test_any_grooming_beats_none(self):
        a = "r5 Tpiste:type=nordic Mw1@"
        b = "r6 Tpiste:type=nordic,piste:grooming=scooter Mw1@"
        self.assertEqual(members_of([a, b]), {"1": "scooter"})

    def test_relation_with_no_grooming_stays_none(self):
        self.assertEqual(members_of(["r5 Tpiste:type=nordic Mw1@"]), {"1": None})


class TagWayLine(unittest.TestCase):
    def test_way_with_no_tags_gets_both(self):
        line = "w1 v2 Thighway=path Nn1,n2"
        self.assertEqual(tag_way_line(line, "classic"), "w1 v2 Thighway=path,piste:type=nordic,piste:grooming=classic Nn1,n2")

    def test_way_keeps_its_own_grooming(self):
        line = "w1 Tpiste:type=nordic,piste:grooming=backcountry Nn1"
        self.assertEqual(tag_way_line(line, "classic"), line)

    def test_way_of_another_type_gains_nordic(self):
        line = "w1 Tpiste:type=downhill Nn1"
        self.assertEqual(tag_way_line(line, None), "w1 Tpiste:type=downhill;nordic Nn1")

    def test_empty_tags(self):
        self.assertEqual(tag_way_line("w1 T Nn1", None), "w1 Tpiste:type=nordic Nn1")

    def test_awkward_grooming_is_escaped(self):
        self.assertEqual(
            tag_way_line("w1 Thighway=path Nn1", "classic; skating"),
            "w1 Thighway=path,piste:type=nordic,piste:grooming=classic;%20%skating Nn1",
        )
        self.assertEqual(
            tag_way_line("w1 Thighway=path Nn1", "a,b"),
            "w1 Thighway=path,piste:type=nordic,piste:grooming=a%2c%b Nn1",
        )

    def test_existing_tags_stay_byte_for_byte(self):
        line = "w1 v2 Tname=Lille%20%vei,note=a%2c%b%3d%c,piste:type=downhill Nn1,n2"
        self.assertEqual(
            tag_way_line(line, "classic"),
            "w1 v2 Tname=Lille%20%vei,note=a%2c%b%3d%c,piste:type=downhill;nordic,piste:grooming=classic Nn1,n2",
        )

    def test_nordic_way_without_grooming_gains_the_relations(self):
        self.assertEqual(
            tag_way_line("w1 Tpiste:type=nordic Nn1", "skating"),
            "w1 Tpiste:type=nordic,piste:grooming=skating Nn1",
        )


class Escape(unittest.TestCase):
    def test_safe_characters_stay(self):
        self.assertEqual(escape("classic+skating:a;b-c_d.e"), "classic+skating:a;b-c_d.e")
        self.assertEqual(escape("fjell\u00f8"), "fjell\u00f8")

    def test_others_become_hex(self):
        self.assertEqual(escape("a b,c=d%e@f"), "a%20%b%2c%c%3d%d%25%e%40%f")


class EnoughTagged(unittest.TestCase):
    def test_threshold_is_ninety_percent(self):
        self.assertTrue(enough_tagged(9311, 9325))
        self.assertTrue(enough_tagged(90, 100))
        self.assertFalse(enough_tagged(89, 100))
        self.assertFalse(enough_tagged(0, 5))


HAVE_OSMIUM = shutil.which("osmium") is not None


@unittest.skipUnless(HAVE_OSMIUM, "osmium not installed")
class RoundTrip(unittest.TestCase):
    def test_member_way_gains_tags_and_the_rest_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            opl = (
                "n1 v1 x10.0 y60.0\nn2 v1 x10.1 y60.0\nn3 v1 x10.2 y60.0\n"
                "w10 v1 Thighway=path,name=Lille%20%vei Nn1,n2\n"
                "w11 v1 Thighway=track Nn2,n3\n"
                "r20 v1 Ttype=route,piste:type=nordic,piste:grooming=classic;%20%skating,"
                "note=a%2c%b Mw10@\n"
            )
            (work / "in.opl").write_text(opl)
            subprocess.run(["osmium", "cat", "-O", "-F", "opl", "-o", str(work / "in.osm.pbf"), str(work / "in.opl")], check=True)
            self.assertEqual(main(str(work / "in.osm.pbf"), str(work / "out.osm.pbf")), 0)
            def dump(name):
                text = subprocess.run(["osmium", "cat", "-f", "opl", str(work / name)],
                                      capture_output=True, text=True, check=True).stdout
                return {line.split(" ")[0]: line for line in text.splitlines()}

            before, after = dump("in.osm.pbf"), dump("out.osm.pbf")
            self.assertEqual(sorted(after), sorted(before))
            for key in before:
                if key != "w10":
                    self.assertEqual(after[key], before[key])
            self.assertIn("piste:type=nordic", after["w10"])
            self.assertIn("piste:grooming=classic;%20%skating", after["w10"])
            self.assertIn("name=Lille%20%vei", after["w10"])


if __name__ == "__main__":
    unittest.main()
