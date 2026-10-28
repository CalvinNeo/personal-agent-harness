# Third-party notices

The AstrBot package is installed separately; this repository does not vendor its
source tree, wheel, dashboard assets, or runtime data.

`patches/astrbot-4.29.3-customizations.patch` modifies
[AstrBot](https://github.com/AstrBotDevs/AstrBot), licensed under GNU AGPL v3.
The patch is provided under that same license. A copy is in
`third_party/AstrBot-AGPL-3.0.txt`. Preserve upstream notices when distributing a
patched AstrBot installation and comply with its license obligations.

QQ SDK (`qq-botpy`) and Codex CLI are installed separately under their respective
upstream licenses. No QQ SDK source modifications were found in the reviewed
deployment. Tests use synthetic identities and generated images.
