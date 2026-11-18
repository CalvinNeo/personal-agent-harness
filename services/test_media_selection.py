import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import douyin_download as dl
import public_web_mcp as web

class MediaSelection(unittest.TestCase):
    def setUp(self):
        dl._resources.clear()
    def media(self, size, kind='video'):
        return {'kind':kind,'size_bytes':size,'urls':['https://v.douyinvod.com/'+str(size)]}
    def result(self, sizes):
        return {'ok':True,'notice':'source notice','source':{'huge':'x'*200000},'subtitles':list(range(30)),
                'description':'keep this','media':[self.media(n) for n in sizes]}
    def test_large_first_variants_do_not_hide_small_streams(self):
        result=web._register_compact_media(self.result([90<<20,85<<20,80<<20,40<<20,12<<20,25<<20,60<<20]))
        self.assertEqual(sorted(m['size_bytes'] for m in result['media']),[12<<20,25<<20,40<<20])
        self.assertEqual(len(dl._resources),3)
    def test_unknown_after_known_usable_and_no_overlarge_handles(self):
        result=dl.register_media(self.result([None,90<<20,20<<20]),max_video=2)
        by_size={m['size_bytes']:m for m in result['media']}
        self.assertFalse(by_size[90<<20]['download_available'])
        self.assertIn('resource_id',by_size[None])
        self.assertIn('resource_id',by_size[20<<20])
        self.assertEqual(result['media'][0]['size_bytes'],20<<20)
    def test_single_response_compacts_source_and_keeps_usable_metadata(self):
        source=self.result([12<<20])
        async def fake(url): return copy.deepcopy(source)
        old=web._read_douyin_video
        web._read_douyin_video=fake
        try: result=asyncio.run(web.read_douyin_video('https://v.douyin.com/example/'))
        finally: web._read_douyin_video=old
        self.assertNotIn('source',result)
        self.assertEqual(result['description'],'keep this')
        self.assertEqual(len(result['subtitles']),8)
        self.assertNotIn('urls',result['media'][0])
        self.assertLess(len(json.dumps(result)),3000)
    def test_only_large_streams_dont_promise_download(self):
        result=web._register_compact_media(self.result([65<<20]))
        self.assertEqual(result['media'],[])
        self.assertEqual(dl._resources,{})

if __name__=='__main__': unittest.main(verbosity=2)
