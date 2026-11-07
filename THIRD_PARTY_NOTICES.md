# Third-party notices

The QEMU patch series is based on QEMU **v10.1.0**, commit
`f8b2f64e2336a28bf0d50b6ef8a7d8c013e9bcf3`, from
<https://gitlab.com/qemu-project/qemu.git>. Existing upstream copyright and
license notices remain in the affected files. QEMU's complete source and
licensing are obtained with the separate upstream checkout.

New SamsungF device implementations and Python helpers are marked
`SPDX-License-Identifier: GPL-2.0-or-later`. The browser viewer and launcher are
provided under the same terms. [LICENSE](LICENSE) contains GPL version 2.

Samsung's firmware, Orsay applications, UI artwork, logos and trademarks retain
their original ownership. Screenshots document the project's observed guest
behavior; the GPL notice for emulator code does not relicense Samsung artwork.
No firmware images, application binaries or recovered certificates are included.

The optional synthetic ARM instruction tests use
[Keystone Engine](https://www.keystone-engine.org/), installed separately through
`requirements-test.txt`. It is a test dependency and is not bundled.

Research references include [SamyGO](https://forum.samygo.tv/),
[linux-chenxing.org](https://linux-chenxing.org/) and
[unixtract](https://github.com/theubusu/unixtract). Their tools are not included
in this prototype, and the project has no implied affiliation with them.
