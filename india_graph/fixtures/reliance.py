"""Offline sample fixture for RELIANCE (MCA CIN: L17110MH1973PLC019786).

Sample data — not real filings. Used to demonstrate the graph -> evidence ->
Q&A flow offline without hitting NSE/BSE/MCA.
"""

RELIANCE_FIXTURE = {
  "mca_company": {
    "sector": "Energy",
    "industry": "Refineries",
    "date_of_incorporation": "1973-05-08",
    "registered_office_address": "Maker Chambers IV, 222 Nariman Point, Mumbai 400021, Maharashtra, India"
  },
  "announcements": [
    {
      "title": "Board meeting outcome - approval of quarterly results",
      "date": "2026-10-22",
      "category": "Board Meeting",
      "attachment": "https://www.nseindia.com/corporates/RELIANCE_Q2FY27_board.pdf",
      "source": "NSE"
    },
    {
      "title": "Dividend declaration for FY2026",
      "date": "2026-08-10",
      "category": "Dividend",
      "attachment": "https://www.bseindia.com/corporates/RELIANCE_dividend_fy26.pdf",
      "source": "BSE"
    },
    {
      "title": "Annual General Meeting notice",
      "date": "2026-08-28",
      "category": "AGM",
      "attachment": "https://www.nseindia.com/corporates/RELIANCE_agm_2026.pdf",
      "source": "NSE"
    }
  ],
  "financials": [
    {
      "metric_name": "NSE Reliance Industries Ltd last price",
      "value": 2950.50,
      "scale": "INR",
      "unit": "INR",
      "period": "latest",
      "fiscal_year": "2027",
      "ticker": "RELIANCE",
      "description": "NSE last price 2950.50 for Reliance Industries Ltd"
    },
    {
      "metric_name": "BSE Q2 FY2027 revenue",
      "value": 3750000000000,
      "scale": "INR",
      "unit": "INR",
      "period": "Q2 FY2027",
      "fiscal_year": "2027",
      "ticker": "RELIANCE",
      "description": "BSE Q2 FY2027 revenue 3750000000000 PAT 320000000000"
    }
  ]
}
