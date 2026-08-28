using System;
using System.IO;
using System.Reflection;
using RotoWeave.Editor;
using NUnit.Framework;
using UnityEngine;

namespace RotoWeave.Tests.Editor
{
    public sealed class RotoWeaveCharacterContractTests
    {
        private static readonly Assembly EditorAssembly =
            typeof(RotoWeaveCharacterImporter).Assembly;

        [Test]
        public void ProductContractIsLayeredDeliveryThree()
        {
            var contract = RequireType("RotoWeave.Editor.RotoWeaveProductContract");
            Assert.That(Constant<string>(contract, "ProductVersion"), Is.EqualTo("4.0.0"));
            Assert.That(Constant<int>(contract, "CharacterPackageFormat"), Is.EqualTo(3));
            Assert.That(
                Constant<string>(contract, "CharacterPackageShape"),
                Is.EqualTo("deduplicated-atlas-v3"));
            Assert.That(Constant<int>(contract, "ScriptedImporterVersion"), Is.EqualTo(8));
        }

        [Test]
        public void ManifestParsesPairedAtlasLayers()
        {
            const string json =
                "{\"formatVersion\":3," +
                "\"packageShape\":\"deduplicated-atlas-v3\"," +
                "\"atlases\":{" +
                "\"base\":[{\"id\":\"atl_0\",\"file\":\"atlases/base/00.png\",\"width\":64,\"height\":32,\"sha256\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}]," +
                "\"emission\":[{\"id\":\"atl_0\",\"file\":\"atlases/emission/00.png\",\"width\":64,\"height\":32,\"sha256\":\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\"}]}}";
            var manifestType = RequireType("RotoWeave.Editor.RotoWeaveCharacterManifest");
            var manifest = JsonUtility.FromJson(json, manifestType);
            var atlasLayers = Field(manifestType, "atlases").GetValue(manifest);
            var atlasLayersType = atlasLayers.GetType();
            var baseAtlases = (Array)Field(atlasLayersType, "base").GetValue(atlasLayers);
            var emissionAtlases =
                (Array)Field(atlasLayersType, "emission").GetValue(atlasLayers);

            Assert.That(baseAtlases.Length, Is.EqualTo(1));
            Assert.That(emissionAtlases.Length, Is.EqualTo(1));
            Assert.That(
                Field(baseAtlases.GetValue(0).GetType(), "id").GetValue(baseAtlases.GetValue(0)),
                Is.EqualTo("atl_0"));
            Assert.That(
                Field(emissionAtlases.GetValue(0).GetType(), "id")
                    .GetValue(emissionAtlases.GetValue(0)),
                Is.EqualTo("atl_0"));
        }

        [Test]
        public void ManifestParsesDeduplicatedSpritesAndAccumulatesFrameDurations()
        {
            const string json =
                "{\"formatVersion\":3," +
                "\"sprites\":[{\"id\":\"spr_shared\",\"atlasId\":\"atl_0\"," +
                "\"rect\":{\"x\":0,\"y\":0,\"width\":16,\"height\":16}," +
                "\"pivot\":{\"x\":0.5,\"y\":0},\"outputScale\":2," +
                "\"sourceSha256\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}]," +
                "\"animations\":[{\"id\":\"action_idle\",\"displayName\":\"Idle\"," +
                "\"durationSeconds\":0.6,\"frames\":[" +
                "{\"id\":\"f0\",\"index\":0,\"spriteId\":\"spr_shared\",\"durationSeconds\":0.1}," +
                "{\"id\":\"f1\",\"index\":1,\"spriteId\":\"spr_shared\",\"durationSeconds\":0.2}," +
                "{\"id\":\"f2\",\"index\":2,\"spriteId\":\"spr_shared\",\"durationSeconds\":0.3}" +
                "]}]}";
            var manifestType = RequireType("RotoWeave.Editor.RotoWeaveCharacterManifest");
            var manifest = JsonUtility.FromJson(json, manifestType);
            var sprites = (Array)Field(manifestType, "sprites").GetValue(manifest);
            var animations = (Array)Field(manifestType, "animations").GetValue(manifest);
            Assert.That(sprites.Length, Is.EqualTo(1));

            var starts = typeof(RotoWeaveCharacterImporter).GetMethod(
                "FrameStartTimes",
                BindingFlags.NonPublic | BindingFlags.Static);
            Assert.That(starts, Is.Not.Null);
            var values = (float[])starts.Invoke(null, new[] { animations.GetValue(0) });
            Assert.That(values.Length, Is.EqualTo(3));
            Assert.That(values[0], Is.EqualTo(0f).Within(0.0001f));
            Assert.That(values[1], Is.EqualTo(0.1f).Within(0.0001f));
            Assert.That(values[2], Is.EqualTo(0.3f).Within(0.0001f));
        }

        [Test]
        public void NonCurrentFormatIsExplicitlyRejected()
        {
            var manifestType = RequireType("RotoWeave.Editor.RotoWeaveCharacterManifest");
            var manifest = JsonUtility.FromJson("{\"formatVersion\":2}", manifestType);
            var validate = typeof(RotoWeaveCharacterImporter).GetMethod(
                "ValidateManifest",
                BindingFlags.NonPublic | BindingFlags.Static);
            Assert.That(validate, Is.Not.Null);

            var invocation = Assert.Throws<TargetInvocationException>(
                () => validate.Invoke(null, new[] { manifest }));
            Assert.That(invocation.InnerException, Is.TypeOf<InvalidDataException>());
            StringAssert.Contains("旧格式", invocation.InnerException.Message);
        }

        [Test]
        public void LayerShadersAndStableRuntimeApiArePresent()
        {
            Assert.That(
                Shader.Find("RotoWeave/Sprites/Premultiplied Base"),
                Is.Not.Null);
            Assert.That(
                Shader.Find("RotoWeave/Sprites/Additive Emission"),
                Is.Not.Null);

            var runtimeType = typeof(RotoWeaveCharacter);
            Assert.That(runtimeType.GetProperty("AnimationNames"), Is.Not.Null);
            Assert.That(runtimeType.GetProperty("DefaultAnimationName"), Is.Not.Null);
            Assert.That(
                runtimeType.GetMethod("HasAnimation", new[] { typeof(string) }),
                Is.Not.Null);
            Assert.That(
                runtimeType.GetMethod("Play", new[] { typeof(string), typeof(float) }),
                Is.Not.Null);
        }

        private static Type RequireType(string name)
        {
            var type = EditorAssembly.GetType(name);
            Assert.That(type, Is.Not.Null, "Missing type " + name);
            return type;
        }

        private static FieldInfo Field(Type type, string name)
        {
            var field = type.GetField(
                name,
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            Assert.That(field, Is.Not.Null, "Missing field " + type.FullName + "." + name);
            return field;
        }

        private static T Constant<T>(Type type, string name)
        {
            var field = type.GetField(
                name,
                BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            Assert.That(field, Is.Not.Null, "Missing constant " + type.FullName + "." + name);
            return (T)field.GetRawConstantValue();
        }
    }
}
