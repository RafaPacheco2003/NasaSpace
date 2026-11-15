import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS
import pipeline

class PipelineTests(unittest.TestCase):
    def test_coordinate_limits(self):
        for ra, dec in [(-1,0),(360,0),(0,91),(float('nan'),0),(0,float('inf'))]:
            with self.assertRaises(ValueError):
                pipeline.coordinate(ra, dec)
        self.assertEqual(pipeline.coordinate(210, -90).dec.deg, -90)

    def test_normalization_invalid_and_constant(self):
        image = np.array([[np.nan, 2], [2, 2]], dtype=float)
        norm, low, high = pipeline.normalize(image)
        np.testing.assert_array_equal(norm, np.zeros((2,2)))
        self.assertEqual((low,high),(2,2))
        with self.assertRaises(ValueError):
            pipeline.normalize(np.full((2,2), np.nan))
        norm, _, _ = pipeline.normalize(np.arange(100).reshape(10,10))
        self.assertEqual(float(norm.min()), 0)
        self.assertEqual(float(norm.max()), 1)

    def test_synthetic_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wcs = WCS(naxis=2)
            wcs.wcs.crpix = [3,3]
            wcs.wcs.crval = [210.80227,54.34895]
            wcs.wcs.cdelt = [-.001,.001]
            wcs.wcs.ctype = ['RA---TAN','DEC--TAN']
            header = wcs.to_header(); header['BUNIT']='MJy/sr'
            plane = np.arange(36, dtype=np.float32).reshape(6,6)
            plane[0,0] = np.nan
            l2 = root/'l2_source.fits'
            fits.HDUList([fits.PrimaryHDU(),fits.ImageHDU(plane, header=header, name='IMAGE')]).writeto(l2)
            cube = np.stack([plane+i for i in range(102)])
            spec = fits.BinTableHDU.from_columns([
                fits.Column(name='WAVELENGTH',format='D',unit='um',array=np.linspace(.75,5,102)),
                fits.Column(name='BANDWIDTH',format='D',unit='um',array=np.full(102,.02))], name='SPECTRAL_CHANNELS')
            source = root/'cube_source.fits'
            fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(cube,header=header,name='IMAGE'),spec,
                          fits.ImageHDU(np.zeros(cube.shape,dtype=np.int32),name='FLAGS'),
                          fits.ImageHDU(np.ones(cube.shape,dtype=np.int32),name='NHITS')]).writeto(source)
            def query(coord, collection):
                return [dict(access_url=str(l2 if collection=='spherex_dr1' else source))]
            with patch.object(pipeline, 'query', side_effect=query):
                result = pipeline.run(210.80227,54.34895,root/'out')
            self.assertEqual(result['status'],'complete', result)
            self.assertEqual(result['allsky']['pixel'],[2,2])
            np.testing.assert_array_equal(np.load(root/'out/samples.npy'), cube[pipeline.CHANNELS-1])
            spectrum = Table.read(root/'out/spectrum.ecsv')
            np.testing.assert_allclose(spectrum['brightness'],cube[:,2,2])
            self.assertEqual(len(spectrum),102)
            with fits.open(root/'out/allsky_cube.fits') as h:
                np.testing.assert_array_equal(h['IMAGE'].data,cube)
                self.assertIn('FLAGS',h)
            for name in ['l2_gray.png','l2_rgb.png','samples.png','cube3d.png','preview3d.npz']:
                self.assertGreater((root/'out'/name).stat().st_size,0)
            self.assertEqual(json.loads((root/'out/manifest.json').read_text())['status'],'complete')

    def test_partial_results(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(pipeline,'l2',side_effect=ValueError('sin L2')), patch.object(pipeline,'allsky',return_value={'shape':[102,6,6]}):
                result=pipeline.run(0,0,root)
            self.assertEqual(result['status'],'partial')
            self.assertEqual(result['l2']['error'],'sin L2')
            self.assertIn('shape',result['allsky'])

    def test_no_cube_candidates(self):
        with tempfile.TemporaryDirectory() as root, patch.object(pipeline,'query',return_value=[]):
            with self.assertRaisesRegex(ValueError,'No se pudo cargar'):
                pipeline.allsky(pipeline.coordinate(0,0),Path(root))

if __name__ == '__main__':
    unittest.main()
