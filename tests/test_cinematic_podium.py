"""Validate the prepared cinematic animation and independent leaderboard text."""
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch
from PIL import Image, ImageChops
import bot
import cinematic_podium as art

FIELDS = (('Minor Mohi | Echo Mohi', 96, (18,2,0)), ('Vecs',72,(12,3,3)), ('Wooooooo',52,(9,2,1)))
REGIONS = ((665,458,1022,542),(715,550,794,598),(839,550,902,598),(954,550,1017,598),
           (247,546,541,624),(288,627,359,670),(397,627,448,670),(488,627,541,670),
           (1148,546,1446,624),(1190,627,1246,670),(1294,627,1349,670),(1391,627,1446,670))

@unittest.skipUnless(art.available(), 'Approved cinematic artwork not installed yet')
class CinematicPodiumTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_boards_use_independent_live_labels(self):
        rows=[dict(rank=i,member_id=i,points=40-i,firsts=5,seconds=2,thirds=1) for i in range(1,4)]
        names={1:'Alpha',2:'Bravo',3:'Charlie'}
        raid_rows=[dict(r,points=r['points']+3) for r in rows]
        with patch.object(bot,'podium_player_names',new=AsyncMock(return_value=names)):
            overall=await bot.Home.for_points(bot.NOVUS_GUILD_ID,rows)
            raids=await bot.RaidTop10.for_points(bot.NOVUS_GUILD_ID,raid_rows)
        self.assertIsNotNone(overall.podium_image)
        self.assertIsNotNone(raids.podium_image)
        self.assertNotEqual(overall.podium_image,raids.podium_image)
        for view in (overall,raids):
            with Image.open(BytesIO(view.podium_image)) as im:
                self.assertEqual((im.format,im.size,im.n_frames,im.info['loop']),('WEBP',art.SIZE,320,0))
                first=im.convert('RGB')
                for frame in (1,11,53,159,200,319):
                    im.seek(frame)
                    current=im.convert('RGB')
                    self.assertEqual(im.info['duration'],25)
                    self.assertIsNotNone(ImageChops.difference(first,current).getbbox())
                    for box in REGIONS:
                        self.assertIsNone(ImageChops.difference(first.crop(box),current.crop(box)).getbbox())

    def test_template_sha_and_unchanged_animation_chunks(self):
        data=art.render_animated(FIELDS)
        expected=[raw for tag,_,raw in art._stage()[1] if tag==b'ANMF'][1:]
        actual=[raw for tag,_,raw in art._chunks(data) if tag==b'ANMF'][1:]
        self.assertEqual(actual,expected)
        self.assertIs(data,art.render_animated(FIELDS))

if __name__ == '__main__':
    unittest.main()
