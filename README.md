# voicemail-manager

A vibecoded 3CX Voicemail Manager to better improve on functionality within the pre-installed voicemail system, while also providing the backend to serve a Yealink XML App for visual voicemail.

-------------------
# App Dependencies
- Python
  - FastAPI: API for UI functioins
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
