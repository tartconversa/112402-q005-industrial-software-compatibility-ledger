# 工业软件版本采集与产线兼容窗口登记

合成工业软件公告、资产构建和生产窗口样例。版本标识按原文保留，窗口日期为本地日期。

All example identifiers and values are synthetic. CSV files use UTF-8, the first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `releases.csv` contains 厂商发布页和公告抓取到的软件版本。 Columns are `release_id`, `vendor`, `product`, `build`, `channel`, `posted`, `state`.
- `devices.csv` contains 设备当前固件构建及产线身份。 Columns are `device`, `product`, `build`, `line`.
- `windows.csv` contains 升级窗口和暂缓审批。 Columns are `exception`, `device`, `from`, `to`, `reason`, `state`.

Python 3 standard library is sufficient to inspect the sample files. From this directory, run `python3 -m unittest discover -s tests -v` to verify only that the supplied files are well-formed and their sample references resolve.
