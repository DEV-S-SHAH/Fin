"""Offline sample fixture for HDFCBANK (MCA CIN: L99999MH1994PLC081704).

Sample data — not real filings. Used to demonstrate the graph -> evidence ->
Q&A flow offline without hitting NSE/BSE/MCA.
"""

HDFCBANK_FIXTURE = {
  "mca_company": {
    "sector": "Financial Services",
    "industry": "Banking",
    "date_of_incorporation": "1994-08-19",
    "registered_office_address": "HDFC Bank House, Senapati Bapat Marg, Lower Parel (W), Mumbai 400013, Maharashtra, India"
  },
  "announcements": [
    {
      "title": "Board meeting outcome - approval of quarterly results",
      "date": "2026-10-15",
      "category": "Board Meeting",
      "attachment": "https://www.bseindia.com/corporates/HDFCBANK_Q2FY27_board.pdf",
      "source": "BSE"
    },
    {
      "title": "Dividend declaration for FY2026",
      "date": "2026-05-20",
      "category": "Dividend",
      "attachment": "https://www.nseindia.com/corporates/HDFCBANK_dividend_fy26.pdf",
      "source": "NSE"
    }
  ],
  "financials": [
    {
      "metric_name": "HDFC Bank Ltd last price",
      "value": 846.0,
      "scale": "INR",
      "unit": "INR",
      "period": "latest",
      "fiscal_year": "2027",
      "ticker": "HDFCBANK",
      "description": "Last price 846.0 for HDFC Bank Ltd"
    },
    {
      "metric_name": "HDFC Bank Ltd Q2 FY2027 revenue",
      "value": 743000000000,
      "scale": "INR",
      "unit": "INR",
      "period": "Q2 FY2027",
      "fiscal_year": "2027",
      "ticker": "HDFCBANK",
      "description": "Q2 FY2027 revenue 743000000000 for HDFC Bank Ltd"
    }
  ]
}
