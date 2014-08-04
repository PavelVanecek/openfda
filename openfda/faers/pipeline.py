#!/usr/bin/python

"""
Pipeline for converting AERS and FAERS data in JSON and
importing into Elasticsearch.
"""

from bs4 import BeautifulSoup
import glob
import logging
import os
from os.path import join, basename, dirname
import re
import requests
import sys
import urllib2

import leveldb
import luigi

from openfda import parallel
from openfda.annotation_table.pipeline import CombineHarmonization
from openfda.faers import annotate
from openfda.faers import xml_to_json


# this should be a symlink to wherever the real data directory is
RUN_DIR = dirname(dirname(os.path.abspath(__file__)))
BASE_DIR = './data/'
FAERS_HISTORIC = ('http://www.fda.gov/Drugs/GuidanceCompliance'
  'RegulatoryInformation/Surveillance/AdverseDrugEffects/ucm083765.htm')
FAERS_CURRENT = ('http://www.fda.gov/Drugs/GuidanceCompliance'
  'RegulatoryInformation/Surveillance/AdverseDrugEffects/ucm082193.htm')

class DownloadDataset(luigi.Task):
  def __init__(self):
    luigi.Task.__init__(self)
    self._faers_current = BeautifulSoup(urllib2.urlopen(FAERS_CURRENT).read())
    self._faers_historic = BeautifulSoup(urllib2.urlopen(FAERS_HISTORIC).read())

  def _fetch(self):
    for page in [self._faers_current.find_all(href=re.compile('.*.zip')),
                 self._faers_historic.find_all(href=re.compile('.*.zip'))]:
      for a in page:
        filename = a.text.split(u'\xa0')[0]
        url = 'http://www.fda.gov' + a['href']
        yield filename, url

  def requires(self):
    return []

  def output(self):
    return luigi.LocalTarget(join(BASE_DIR, 'faers/raw'))

  def run(self):
    os.system('mkdir -p "%s"' % self.output().path)
    for filename, url in list(self._fetch()):
      target_name = join(BASE_DIR, 'faers/raw', filename)
      os.system("curl '%s' > '%s'" % (url, target_name))


class ExtractZip(luigi.Task):
  def requires(self):
    return DownloadDataset()

  def output(self):
    return luigi.LocalTarget(join(BASE_DIR, 'faers/extracted'))

  def run(self):
    src_dir = self.input().path
    os.system('mkdir -p "%s"' % self.output().path)
    for zip_file in glob.glob(src_dir + '/*.[Zz][Ii][Pp]'):
      extract_dir = join(self.output().path, basename(zip_file).split('.')[0])
      os.system('unzip -d %(extract_dir)s %(zip_file)s' % locals())

    # AERS SGM records don't always properly escape &
    for sgm in glob.glob(self.output().path + '/*/sgml/*.SGM'):
      os.system('sed -i -e "s/&/&amp;/" %s' % sgm)


class XML2JSON(luigi.Task):
  def requires(self):
    return ExtractZip()

  def output(self):
    return luigi.LocalTarget(join(BASE_DIR, 'faers/json/'))

  def run(self):
    print 'Pipelining....'
    # AERS_SGML_2007q4.ZIP has files in sqml
    filenames = glob.glob(self.input().path + '/AERS_SGML_*/s[gq]ml/*.SGM')
    filenames.extend(glob.glob(self.input().path + '/FAERS_XML*/xml/*.xml'))

    input_shards = []
    for filename in filenames:
      if 'test' in filename.lower():
        continue
      logging.info('Adding input file to pool: %s', filename)
      input_shards.append(filename)

    parallel.mapreduce(
      parallel.Collection.from_list(input_shards),
      xml_to_json.ExtractSafetyReportsMapper(),
      xml_to_json.MergeSafetyReportsReducer(),
      self.output().path,
      10)


class AnnotateJSON(luigi.Task):
  def requires(self):
    return [CombineHarmonization(), XML2JSON()]

  def output(self):
    return luigi.LocalTarget(join(BASE_DIR, 'faers/annotated/'))

  def run(self):
    harmonized_file = self.input()[0].path
    parallel.mapreduce(
      parallel.Collection.from_sharded(self.input()[1].path),
      annotate.AnnotateMapper(harmonized_file),
      parallel.IdentityReducer(),
      self.output().path,
      num_shards=10,
      map_workers=2)


class ResetElasticSearch(luigi.Task):
  def requires(self):
    return AnnotateJSON()

  def output(self):
    return luigi.LocalTarget('/tmp/elastic.initialized')

  def run(self):
    os.system(join(RUN_DIR, 'faers/clear_and_load_mapping.sh'))
    os.system('touch "%s"' % self.output().path)


class LoadJSONMapper(parallel.Mapper):
  def map_shard(self, map_input, map_output):
    json_batch = []
    def _post_batch():
      response = requests.post(
        'http://localhost:9200/drugevent/safetyreport/_bulk',
        data='\n'.join(json_batch))
      del json_batch[:]
      if response.status_code != 200:
        logging.info('Bad response: %s', response)

    for i, (case_number, event_raw) in enumerate(map_input):
      json_batch.append('{ "index" : {} }')
      json_batch.append(event_raw)
      if len(json_batch) > 1000:
        _post_batch()

    _post_batch()


class LoadJSON(luigi.Task):
  def requires(self):
    return [ResetElasticSearch(), AnnotateJSON()]

  def output(self):
    return luigi.LocalTarget('/tmp/elastic.done/')

  def run(self):
    parallel.mapreduce(
      parallel.Collection.from_sharded(self.input()[1].path),
      LoadJSONMapper(),
      parallel.NullReducer,
      output_prefix=self.output().path,
      num_shards=1,
      map_workers=1)


if __name__ == '__main__':
  logging.basicConfig(
    stream=sys.stderr,
    format='%(created)f %(filename)s:%(lineno)s [%(funcName)s] %(message)s',
    level=logging.DEBUG)

  luigi.run()
