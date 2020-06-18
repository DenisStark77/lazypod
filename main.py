# main.py
import os
import telegram
import traceback
import requests
from requests.auth import HTTPBasicAuth
import json
import re
import textrazor

# Voice recognition and cloud storage
#from google.cloud import speech
from google.cloud import speech_v1p1beta1
from google.cloud.speech_v1p1beta1 import enums
from google.cloud.speech_v1p1beta1 import types
from google.cloud import storage


def webhook(request):
    try:
        bot = telegram.Bot(token=os.environ["TELEGRAM_TOKEN"])
        client = storage.Client()
        bucket = client.get_bucket('lazypod-podcasts')

        if request.method == "POST":
            update = telegram.Update.de_json(request.get_json(force=True), bot)
            if update.channel_post is not None and update.channel_post.voice is not None:
                msg = update.channel_post
                audio_file = '%d/%d.mp3' % (msg.chat_id, msg.message_id)
                json_file = '%d/%d.json' % (msg.chat_id, msg.message_id)

                json_blob = bucket.blob(json_file)

                # If recognition done before just return
                if json_blob.exists():
                    return "ok"
                else:
                    json_blob.upload_from_string(json.dumps({'run': True}).encode('utf8'), 'application/json')

                # If audio not yet at GCS upload it
                audio_blob = bucket.blob(audio_file)
                if not audio_blob.exists():
                    print('Downloading')
                    file = update.channel_post.voice.get_file()
                    ba = file.download_as_bytearray()

                    print('Audio file type:', type(ba))
                    print('File downloaded: size %d' % len(ba))


                    # Uploading to GCS
                    print('File uploading to %s' % audio_file)
                    audio_blob = bucket.blob(audio_file)
                    audio_blob.upload_from_string(bytes(ba), content_type='audio/mpeg3')
                    print('File uploaded GCS')

                audio_uri = 'gs://lazypod-podcasts/' + audio_file
                print('File voice recognizing')
                text = voice_recognize(audio_uri)
                print('File voice recognized')

                # Get summary of the text
                summary = get_summary(text, sentences=4)

                # Extract tags
                tags = get_tags(text)
                tags = ', '.join(tags)

                # Get headline for the podcast
                headline = get_summary(text, sentences=1)
                if len(headline) > 0:
                    if headline[-1] == '.':
                        headline = headline[:-1]

                if len(summary) > 500:
                    summary = summary[:500]

                caption = '<b>' + headline + '</b>\n' + summary + '\n' + tags

                res = msg.edit_caption(caption=caption, parse_mode='HTML')
                print('Result:', res)

                # Store JSON to GCS
                data = {
                    'caption': caption,
                    'text': text,
                    'summary': summary,
                    'tags': tags,
                    'headline': headline,
                    'filename': audio_file,
                    'published': False
                }
                json_blob.upload_from_string(json.dumps(data).encode('utf8'), 'application/json')
                print('JSON stored')
            # Post are edited (only consider posts with voice)
            elif update.edited_channel_post is not None and update.edited_channel_post.voice is not None:
                msg = update.edited_channel_post
                audio_file = '%d/%d.mp3' % (msg.chat_id, msg.message_id)
                json_file = '%d/%d.json' % (msg.chat_id, msg.message_id)
                json_blob = bucket.blob(json_file)

                # Parse new caption
                headline, summary, tags = msg.caption.split('\n')[0:3]
                print('Headline:', headline)
                print('Summary:', summary)
                print('Tags:', tags)

                # If json for this message does't exist ignore it
                if json_blob.exists():
                    data = json.loads(json_blob.download_as_string())
                    data['summary'] = summary
                    data['tags'] = tags
                    data['headline'] = headline
                else:
                    print('JSON does not exist for [%s]' % json_file)
                    data = {
                        'caption': msg.caption,
                        'text': summary,
                        'summary': summary,
                        'tags': tags,
                        'headline': headline,
                        'filename': audio_file,
                        'published': False
                    }

                # Only publish if text are changes and not yet published
                if not json_blob.exists() or not data['published']:
                    # Publish podcast in podbean
                    client_url = 'https://storage.cloud.google.com/lazypod-podcasts/' + audio_file
                    player_url = publish_podcast(client_url, headline, summary)
                    data['player_url'] = player_url
                    data['published'] = len(player_url) > 0

                    # Store updated podcast data
                    json_blob.upload_from_string(json.dumps(data).encode('utf8'), 'application/json')
                    print('Data stored in JSON')

            # Reply with the same message
            # bot.sendMessage(chat_id=chat_id, text=update.message.text)
        return "ok"
    except Exception as e:
        print('Exception in webhook [%s]' % e)
        traceback.print_exc()
        return "ok"


def voice_recognize(storage_uri):
    """
    Performs synchronous speech recognition on an audio file

    Args:
      storage_uri URI for audio file in Cloud Storage, e.g. gs://[BUCKET]/[FILE]
    """

    client = speech_v1p1beta1.SpeechClient()

    # storage_uri = 'gs://cloud-samples-data/speech/brooklyn_bridge.mp3'

    # Encoding of audio data sent. This sample sets this explicitly.
    # This field is optional for FLAC and WAV audio formats.
    config = types.RecognitionConfig(
        encoding=enums.RecognitionConfig.AudioEncoding.MP3,
        sample_rate_hertz=44100,
        language_code='en-US',
        # Enable automatic punctuation
        enable_automatic_punctuation=True)

    audio = {"uri": storage_uri}

    response = client.recognize(config, audio)

    result = [r.alternatives[0].transcript for r in response.results]

    return ' '.join(result)


def publish_podcast(url, title, text):
    endpoint = 'https://api.podbean.com/v1/oauth/token'
    # TODO: Remove API keys from code
    res = requests.post(endpoint, data={'grant_type': 'client_credentials'}, auth=HTTPBasicAuth('c8b13fb305cbd42cd2511', 'd1d237d88d0a20d8b9495'))

    if not res.ok:
        print('HTTP error:', res.status_code, res._content)
        return ''

    res_json = json.loads(res._content)

    data = {
        'access_token': res_json['access_token'],
        'title': title,
        'content': text,
        'status': 'publish',
        'type': 'public',
        'remote_media_url': url
    }
    endpoint = 'https://api.podbean.com/v1/episodes'
    res = requests.post(endpoint, data=data)
    if not res.ok:
        print('HTTP error:', res.status_code, res._content)
        return ''

    res_json = json.loads(res._content)
    if 'episode' in res_json and 'player_url' in res_json['episode']:
        return res_json['episode']['player_url']

    return ''


def get_summary(text, sentences=1):
    endpoint = 'https://api.meaningcloud.com/summarization-1.0'

    data = {'key': 'aca132c0df0674b088db6be0a960b1be',
            'of': 'JSON',
            'txt': text,
            'sentences': sentences
            }

    res = requests.post(endpoint, data=data)

    if res.ok:
        res_json = json.loads(res._content)
        return res_json['summary']
    else:
        return ''


def get_tags(text):
    textrazor.api_key = "631d67844c4e5bf22a4dfe37afcd0f08a3c330b54a8ca798a0970846"

    client = textrazor.TextRazor(extractors=["topics"])

    # classifiers=['textrazor_mediatopics', 'textrazor_newscodes', 'textrazor_iab', 'textrazor_iab_content_taxonomy']
    client.set_classifiers(['textrazor_iab'])

    response = client.analyze(text)
    if not response.ok:
        print(response.error)
        print(response.message)
        return []

    tags = []
    for c in response.categories():
        if c.score > 0.5:
            category = re.sub(r"[>]+", "/", c.label)
            tag = category.split('/')[-1]
            tag = re.sub(r"[\s&]+", "_", tag)

            if len(tag) > 0:
                tag = '#' + tag.lower()
                tags.append(tag)

    for c in response.topics():
        if c.score == 1.0:
            tag = c.label
            tag = re.sub(r"[\s&]+", "_", tag)
            tag = '#' + tag.lower()
            tags.append(tag)

    return tags
