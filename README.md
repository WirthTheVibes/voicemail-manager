# Voicemail Manager

<img width="1912" height="914" alt="image" src="https://github.com/user-attachments/assets/e5ca3395-ee17-459b-9ccb-106ec6f25340" />
A vibecoded web app to manage 3CX Voicemail, improving on functionality within the pre-installed voicemail system, while also providing the backend to serve a Yealink XML App for visual voicemail.

-------------------
# Features
- Group-based voicemail boxes to fit within your business call flow requirements.
- API for Yealink XML apps to provide a visual voicemail experience for Yealink devices.
- Click-to-call within app to call back a customer.
- Voicemail delegation to end users, allowing users to manage and review other mailboxes within app.
- Timestamp tracking for when staff review voicemails and follow up with callers.
- On-CPU transcription for voicemails.

-------------------
# Disclaimer
This project is an independent, unofficial tool and is not affiliated with, endorsed by, or supported by 3CX in any way.

This application works by directly reading from and modifying the 3CX PostgreSQL database, which is not a supported or documented integration method. As a result:

Use of this software may violate the 3CX Terms of Use and/or End User License Agreement. It is your responsibility to review 3CX's terms and determine whether use of this tool is permitted in your environment.
No warranty is provided, express or implied, regarding the functionality, stability, or supportability of your 3CX installation while using this software. Direct Database modifications may cause unexpected behavior, data corruption, loss of functionality, or issues with future 3CX updates.

Using this tool may void or complicate your eligibility for official 3CX support.
The authors and contributors accept no liability for any damages, data loss, service outages, or breach-of-terms consequences arising from the use of this software.

Use entirely at your own risk. Always back up your database before use, and test in a non-production environment first.

-----------------------
# App Dependencies
- Python
  - FastAPI: API for UI functions
  - Uvicorn: Web Server
  - psycopg2-binary: reading/writing  3CX Postgres Database
  - itsdangerous: session tokens
  - requests: 3CX Admin Spoofing
  - faster-whisper: on-server transcription
  - pywebpush: browser push notifications (not implemented yet)
  - pyjwt[crypto]: JWT signing for MS Auth and Yealink XML Browser sessions
  - pyotp: TOTP codes for the 3CX admin login when 2FA is enrolled on that extension
- PJSUA
- 3CX
  - Local CDR file for accurate call path tracing.
  - 1+ free extensions for voicemail manager phone extension and group voicemail boxes. 
--------------------
# Tested and confirmed Yealink Models
- T54W
- T46U/T46S
- T33G
- T73W
- T74W
--------------------
# Recommended 3CX Prep

1. Disable all voicemail notifications for 3CX as this app will have its own notifications and MWI management, set all users to "No email notification".
2. Set all phones to utilize a custom provisioning template - copy 3CX's existing template then modify with the following settings at the bottom of the file:
```
#######################################################################################
##                                    CUSTOM                                         ##          
#######################################################################################

#VM Manager for Voicemail Button - double press will override and call 3CX voicemail ext.
#The models in the README.md have been tested with programablekey.18 being the voicemail key on the model - may have to search for your speicifc model.
programablekey.18.line = 0
programablekey.18.type = 27
programablekey.18.value = https://<3cx host uri>/vm-manager/vvm/menu?ext=%%extension_number%%

#VM Manager Menu Phone Book - used to organize contacts by department and to remove CRM contacts being exposed.
remote_phonebook.data.2.url = https://<3cx host uri>/vm-manager/menu.xml
remote_phonebook.data.2.name = VM Manager Phone Book

#VM Manager Phone Book - the actual phone book
programablekey.2.type = 22
programablekey.2.line = 2
programablekey.2.value = VM Manager Phone Book
programablekey.2.history_type =
programablekey.2.label = Phone Book
programablekey.2.xml_phonebook =

#MWI light only for voicemails - optional, feedback from end users I got is that they "do not know if the light actually means something to them" due to queues and ring groups also signaling MWI.
phone_setting.missed_call_power_led_flash.enable = 0
```
3. Enable 3CX CDR under Admin > Advanced > CDR - single file for all calls.

----------------------
# Known issues
- Visual voicemail will play the audio file twice (known bug by Yealink, awaiting new firmware).
