# Statement Miner

Convert Chase PDF statement state into CSV.

## Installation

    $ pipenv install --three

## Usage

First you have to rename Chase statements to match the file format specified
in `EXTRACTORS`. For me that was:

    $ mv 20180117-statements-x1234-.pdf 2018-01-17-statements-1234.pdf

Or just fix the regexp.

Then run the extractor like:

    $ pipenv run python3 extract.py ~/Downloads/2018-01-17-statements-5969.pdf

You can redirect stdout if you want to capture the CSV content.

    $ pipenv ... >test.csv
