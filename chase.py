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


class ChaseConverter(converter.PDFLayoutAnalyzer):
  """Converts Chase statements to CSV."""

  _TREE_LOG = logging.getLogger('%s.tree' % __name__)

  def __init__(self, rsrcmgr, statement_date):
    super(ChaseConverter, self).__init__(
        rsrcmgr, laparams=layout.LAParams(all_texts=True, detect_vertical=True))
    self.boxes_by_page = {}
    self.header = None
    self.items = {}
    self.expenses = []
    self.statement_date = statement_date

  REGEXES = {
      r'[0-9]*\.[0-9]{2}': 'price',
      r'[0-9]{2}/[0-9]{2}': 'date',
  }

  def _classify_container(self, c):
    for r in self.REGEXES:
      matches = [t for t in c if re.match(r, t.get_text().strip('\n'))]
      if len(matches) >= len(c) - 1:
        _LOG.debug('%s: %d/%d %s', self.REGEXES[r], len(matches), len(c), iter(c).__next__().get_text())
        return matches, self.REGEXES[r]

    return c, ''

  def process_item(self, ltpage, y0, item, level):
    if isinstance(item, layout.LTText):
      item_text = item.get_text()
      if self.header is None and 'ACCOUNT ACTIVITY' in item_text:
        _LOG.debug('Found header (%s): %r', ltpage.pageid, item)
        self.header = item

      if 'Account Number: ' in item_text:
        self.account_last4 = re.sub(
            '[^\d]', '', item_text.split(':', 1)[1])[-4:]
        _LOG.debug('Found last-4: %s', self.account_last4)

    if isinstance(item, layout.LTComponent):
      if isinstance(item, layout.LTTextBoxHorizontal):
        matches, statement_type = self._classify_container(item)
        for m in matches:
          m_y0 = y0 + m.y0
          _LOG.debug('%s[%f]: %s', statement_type, m_y0, m.get_text().strip('\n'))
          line_dict = self.items.setdefault(m_y0, {})
          line_dict[statement_type] = m.get_text().strip('\n')

    if isinstance(item, layout.LTContainer):
      self._TREE_LOG.debug('%s+ %r', ' ' * level, item)
      for child in item:
        if isinstance(child, layout.LTContainer):
          self.process_item(ltpage, y0 + child.y0, child, level + 2)
    else:
      pass
#      self._TREE_LOG.debug('%s> %r', ' ' * level, item)

  def receive_layout(self, ltpage):
    self.items = {}
    self.header = None
    self.process_item(ltpage, 0, ltpage, 0)

    if not self.header:
      _LOG.debug('Page %s: no header', ltpage.pageid)
      return []

    _LOG.debug('Finding items >= %f', self.header.y0)
    for y0 in sorted(self.items):
      items = self.items[y0]
      if len(items) > 1:
        _LOG.debug('line %f: %r', y0, items)

      if (len(items) != 3 or
          'price' not in items or
          'date' not in items or
          '' not in items):
        continue
      time_tuple = list(time.strptime(items['date'], '%m/%d'))
      time_tuple[0] = self.statement_date.year
      if time_tuple[1] == 12 and self.statement_date.month == 1:
        time_tuple[0] -= 1
      date = datetime.datetime(*time_tuple[:6])
      self.expenses.append(Expense(
          date=date, merchant=items[''], price=decimal.Decimal(items['price']),
          account=self.account_last4 or ''))
      _LOG.debug('Expense: %s %s %s', items['date'], items[''], items['price'])

  # Some dummy functions to save memory/CPU when all that is wanted
  # is text.  This stops all the image and drawing output from being
  # recorded and taking up RAM.
  def render_image(self, name, stream):
    return

  def paint_path(self, gstate, stroke, fill, evenodd, path):
    return


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


def _ProcessDoc(statement_date, doc, csv):
  rsrc_mgr = pdfinterp.PDFResourceManager()
  device = ChaseConverter(rsrc_mgr, statement_date)
  interp = pdfinterp.PDFPageInterpreter(rsrc_mgr, device)
  for page in pdfpage.PDFPage.create_pages(doc):
    interp.process_page(page)

  _LOG.info('Account %s on %s: %d expenses, $%.2f',
            device.account_last4, statement_date, len(device.expenses),
            sum(e.price for e in device.expenses))
  return device.expenses


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

    statement_date = datetime.datetime.strptime(
        os.path.basename(st.name).split('-statements', 1)[0], '%Y-%m-%d')
    expenses.extend(_ProcessDoc(statement_date, doc, args.csv))

  w = csv.DictWriter(args.csv, fieldnames=Expense._fields)
  w.writeheader()
  for e in sorted(expenses, key=lambda e: e.date):
    w.writerow(e._asdict())

  _LOG.info('Found %d expenses', len(expenses))


if __name__ == '__main__':
  Main()
