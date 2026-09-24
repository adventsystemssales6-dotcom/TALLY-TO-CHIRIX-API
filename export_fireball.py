#!/usr/bin/env python3
import urllib.request

xml_query = """<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Export Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <EXPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>List of Accounts</REPORTNAME>
    <STATICVARIABLES>
     <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
     <ACCOUNTTYPE>Ledgers</ACCOUNTTYPE>
    </STATICVARIABLES>
   </REQUESTDESC>
  </EXPORTDATA>
 </BODY>
</ENVELOPE>"""

req = urllib.request.Request("http://127.0.0.1:9000", data=xml_query.encode("utf-8"), headers={"Content-Type": "text/xml"})
res = urllib.request.urlopen(req)
text = res.read().decode("utf-8", errors="ignore")
idx = text.find("FIREBALL TECHNOLOGIES")
if idx != -1:
    print("FOUND FIREBALL TECHNOLOGIES XML:")
    print(text[idx-100:idx+2000])
else:
    print("Not found in List of Accounts")
