EPUB bilingual translator - multi-provider edition

Supported providers with verified free capacity >= 500k characters (2026-09-02):
1. DeepL API Free - 1,000,000 chars/month
2. Google Cloud Translation - first 500,000 chars/month free
3. Microsoft Translator - 2,000,000 chars/month free
4. Amazon Translate - 2,000,000 chars/month free for 12 months
5. Baidu Translate - 1,000,000 chars/month (plan eligibility applies)
6. Alibaba Cloud Machine Translation - 1,000,000 chars/month free

IMPORTANT:
- The program saves credentials ONLY AFTER a successful API validation.
- Wrong credentials are never saved.
- Each run lets you choose a provider.
- GPU/CUDA is not used.
- Use the ORIGINAL English EPUB as input. Do not feed *_EN-ZH.epub or *.tmp.epub back in.
- The output is *_EN-ZH.epub in the same folder as the input unless an output path is supplied.

For AWS, create an IAM user/role with permission for translate:TranslateText and provide Access Key ID, Secret Access Key, and region.
For Microsoft Translator, provide the Azure Translator key and resource region.
For Google, enable Cloud Translation API and provide an API key permitted to call it.
For Baidu, provide APP ID and Secret Key for the translation service.
For Alibaba Cloud, provide AccessKey ID and AccessKey Secret with Machine Translation permission.
