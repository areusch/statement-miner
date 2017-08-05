import argparse
import datetime
import decimal
import csv
import collections
import io
import logging
import os.path
import re
import time

from pdfminer import converter
from pdfminer import layout
from pdfminer import pdfdocument
from pdfminer import pdfinterp
from pdfminer import pdfpage
from pdfminer import pdfparser
from pdfminer import utils


_LOG = logging.getLogger(__name__)


Expense = collections.namedtuple('Expense', 'date merchant price account')


class LayoutVisitorDriver(converter.PDFLayoutAnalyzer):

  _LOG = logging.getLogger('%s.LayoutVisitorDriver' % __name__)

  def __init__(self, rsrcmgr, visitor):
    super(LayoutVisitorDriver, self).__init__(
        rsrcmgr, laparams=visitor.LAPARAMS)
    self.visitor = visitor

  def visit_layout(self, y0, l, level):
    method = None
    for cls in l.__class__.__mro__:
      method_name = 'Visit%s' % cls.__name__
      method = getattr(self.visitor, method_name, None)
      if method:
        method(y0, l)
        break

    try:
      i = iter(l)
      self._LOG.debug('%s+ (%3.2f)%r', ' ' * (level * 2), y0, l)

    except TypeError:
      self._LOG.debug('%s> (%3.2f)%r', ' ' * (level * 2), y0, l)
      return

    for e in i:
      self.visit_layout(y0 + l.y0, e, level + 1)

  def receive_layout(self, ltpage):
    self.visit_layout(0, ltpage, 0)


LabelledText = collections.namedtuple('LabelledText', 'label text')


class LabelSet(list):

  def key_set(self):
    return sorted(i.label for i in self)

  def __contains__(self, key):
    return any(i.label == key for i in self)

  def __getitem__(self, key):
    if isinstance(key, str):
      for i in self:
        if i.label == key:
          return i

      raise KeyError(key)

    return list.__getitem__(self, key)


class BillingDetailExtractorBase(object):
  """Common functions that search for detailed billing lines in statements."""

  _BD_LOG = logging.getLogger('%s.BillingDetailExtractorBase' % __name__)

  def __init__(self):
    super(BillingDetailExtractorBase, self).__init__()
    self.Reset()

  def Reset(self):
    self.labelled_lines = {}

  TEXT_CLASSIFIERS = {}

  def _ClassifyTextBox(self, c):
    for r in self.TEXT_CLASSIFIERS:
      matches = [t for t in c if re.match(r, t.get_text())]
      if len(matches) and len(matches) >= len(c) - 1:
        self._BD_LOG.debug(
            '%s: %d/%d %s',
            self.TEXT_CLASSIFIERS[r], len(matches), len(c),
            iter(c).__next__().get_text())
        return matches, self.TEXT_CLASSIFIERS[r]

    return c, ''

  def VisitLTTextBoxHorizontal(self, y0, item):
    matches, label = self._ClassifyTextBox(item)
    for m in matches:
      m_y0 = y0 + m.y0
      self._BD_LOG.debug('%s[%f]: %s', label, m_y0, m.get_text())
      self.labelled_lines.setdefault(m_y0, LabelSet()).append(
          LabelledText(label=label, text=m.get_text()))

  def ParseLines(self):
    self._BD_LOG.debug('Labelled %d positions', len(self.labelled_lines))
    y0s = list(sorted(self.labelled_lines))
    for i, y0 in enumerate(y0s):
      self.ParseLine(y0s, i, y0, self.labelled_lines[y0])


class ChaseDetailExtractor(BillingDetailExtractorBase):
  """Extracts detail from Chase statements."""

  _LOG = logging.getLogger('%s.ChaseDetailExtractor' % __name__)

  LAPARAMS = layout.LAParams(all_texts=True, detect_vertical=True)

  EXCLUDED_MERCHANTS = ['AUTOMATIC PAYMENT - THANK YOU']

  TEXT_CLASSIFIERS = {
      r'\$?\-?([0-9]*\.[0-9]{2})\n': 'price',
      r'([0-9]{2}/[0-9]{2})\n': 'date',
  }

  def __init__(self, match):
    super(ChaseDetailExtractor, self).__init__()
    self.statement_date = datetime.datetime.strptime(
        match.group('date'), '%Y-%m-%d')
    self.account = None

  def Reset(self):
    super(ChaseDetailExtractor, self).Reset()
    self.expenses = []

  def VisitLTTextBoxHorizontal(self, y0, item):
    item_text = item.get_text()
    if 'Account Number: ' in item_text:
      self.account = re.sub('[^\d]', '', item_text.split(':', 1)[1])[-4:]
      self._LOG.debug('Found last-4: %s', self.account)

    super(ChaseDetailExtractor, self).VisitLTTextBoxHorizontal(y0, item)

  def receive_layout(self, ltpage):
    ret_val = super(ChaseDetailExtractor, self).receive_layout(ltpage)

  def ParseLine(self, y0s, i, y0, items):
    if len(items) != 3:
      return

    if items.key_set() != ['', 'date', 'price']:
      return

    self._LOG.debug('line: %r', items.key_set())

    time_tuple = list(time.strptime(items['date'].text, '%m/%d\n'))
    time_tuple[0] = self.statement_date.year
    if time_tuple[1] == 12 and self.statement_date.month == 1:
      time_tuple[0] -= 1
    date = datetime.datetime(*time_tuple[:6])
    exp = Expense(
        date=date,
        merchant=items[''].text.strip('\n'),
        price=decimal.Decimal(items['price'].text.strip('\n')),
        account=self.account or '')

    if exp.merchant in self.EXCLUDED_MERCHANTS:
      return

    self.expenses.append(exp)
    self._LOG.debug('Expense: %r', exp)


class AmexDetailExtractor(BillingDetailExtractorBase):
  """Extracts detail from Chase statements."""

  _LOG = logging.getLogger('%s.AmexDetailExtractor' % __name__)

  LAPARAMS = layout.LAParams(all_texts=True, detect_vertical=True)

  TEXT_CLASSIFIERS = {
      r'\$?[0-9]*\.[0-9]{2}\n': 'price',
      r'[0-9]{2}/[0-9]{2}/[0-9]{2}\n': 'date',
  }

  NAME_Y_LIMIT = 2.0

  def __init__(self, match):
    super(AmexDetailExtractor, self).__init__()
    self.statement_date = datetime.datetime.strptime(
        match.group('date'), '%b %Y')
    self.account = None

  def Reset(self):
    super(AmexDetailExtractor, self).Reset()
    self.expenses = []

  def VisitLTTextBoxHorizontal(self, y0, text):
    item_text = text.get_text()
    if 'Account Ending' in item_text:
      self.account = item_text.strip('\n').split('-', 1)[1]
      _LOG.debug('Found AMEX last-5: %s', self.account)

    super(AmexDetailExtractor, self).VisitLTTextBoxHorizontal(y0, text)

  def ParseLine(self, y0s, i, y0, items):
    if 'date' not in items or 'price' not in items:
      return

    next_y0 = y0s[i + 1]
    next_items = self.labelled_lines[next_y0]
    if next_y0 - y0 > self.NAME_Y_LIMIT:
      self._LOG.error('Bailing on transaction @ %f: %r', next_y0 - y0, items)
      return

    merchant = next_items[0].text.strip('"\n')

    time_tuple = list(time.strptime(items['date'].text, '%m/%d/%y\n'))
    if time_tuple[1] == 12 and self.statement_date.month == 1:
      time_tuple[0] -= 1
    date = datetime.datetime(*time_tuple[:6])
    exp = Expense(
        date=date,
        merchant=merchant,
        price=decimal.Decimal(items['price'].text[1:].strip('\n')),
        account=self.account or '')
    self.expenses.append(exp)
    self._LOG.debug('Expense: %r', exp)


def ParseArgs():
  parser = argparse.ArgumentParser()
  parser.add_argument('statement',
                      nargs='+',
                      type=argparse.FileType('rb'),
                      help='The statement to parse')
  parser.add_argument('csv',
                      nargs='?',
                      default='-',
                      type=argparse.FileType('w+'),
                      help='CSV file to append to')
  return parser.parse_args()


def _ProcessDoc(file_name, extractor, doc):
  rsrc_mgr = pdfinterp.PDFResourceManager()
#  extractor._LOG.setLevel(logging.DEBUG)
  device = LayoutVisitorDriver(rsrc_mgr, extractor)
  interp = pdfinterp.PDFPageInterpreter(rsrc_mgr, device)

  expenses = []
  for page in pdfpage.PDFPage.create_pages(doc):
    extractor.Reset()
    interp.process_page(page)
    extractor.ParseLines()
    expenses.extend(extractor.expenses)

  _LOG.info('%s: Account %s on %s: %d expenses, $%.2f',
            os.path.basename(file_name), extractor.account,
            extractor.statement_date, len(expenses),
            sum(e.price for e in expenses))
  return expenses


EXTRACTORS = {
  r'Statement_(?P<date>[A-Za-z]{3} [0-9]{4}).pdf': AmexDetailExtractor,
  r'(?P<date>[\d]{4}-[\d]{2}-[\d]{2})-statements-[0-9]{4}.pdf': ChaseDetailExtractor,
}


def Main():
  logging.basicConfig()
  _LOG.setLevel(logging.INFO)
  args = ParseArgs()
  expenses = []
  for st in args.statement:
    parser = pdfparser.PDFParser(st)
    doc = pdfdocument.PDFDocument(parser)
    if not doc.is_extractable:
      _LOG.error('Doc not extractable: %s', st.name)
      continue

    for file_re, extractor_cls in EXTRACTORS.items():
      m = re.match(file_re, os.path.basename(st.name))
      if m:
        extractor = extractor_cls(m)

    expenses.extend(_ProcessDoc(st.name, extractor, doc))

  w = csv.DictWriter(args.csv, fieldnames=Expense._fields)
  w.writeheader()
  for e in sorted(expenses, key=lambda e: e.date):
    w.writerow(e._asdict())

  _LOG.info('Found %d expenses', len(expenses))


if __name__ == '__main__':
  Main()
